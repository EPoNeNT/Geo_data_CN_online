import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import certitude_discovery as discovery


class Session:
    created = []
    def __init__(self): self.headers = {}; self.closed = False; self.calls = []; self.created.append(self)
    def close(self): self.closed = True


class Store:
    def __init__(self, scans=None): self.scans = dict(scans or {}); self.events = []; self.saved_checkers = []
    def load_activity_scans(self): return self.scans
    def save_checker(self, row): self.saved_checkers.append(dict(row)); self.events.append(("checker", row["checkerId"]))
    def save_activity_scan(self, scope, row):
        self.scans[scope] = dict(row); self.events.append(("scan", scope, dict(row)))


class Fetcher:
    def __init__(self, pages): self.pages = pages; self.calls = []
    def get(self, session, url, hosts):
        session.calls.append(("GET", url))
        self.calls.append(("GET", url))
        val = self.pages[url]
        if isinstance(val, Exception): raise val
        return val, url
    def post(self, session, url, hosts, data):
        session.calls.append(("POST", dict(data)))
        self.calls.append(("POST", data["filter_country"]))
        return self.pages["POST:" + data["filter_country"]], "https://www.certitudes.org/"


def page(country, rows=(), next_url=None):
    return {"selectedCountry": country, "filters": {"filter_region": "All", "filter_quadrangle": "All",
            "filter_owner": "", "filter_user": ""}, "sort": "dat", "direction": "desc",
            "rows": list(rows), "nextUrl": next_url}


def row(cid, parent="GC1", published="2026-11-02T00:00:00Z"):
    return {"checkerId": cid, "sourceUrl": "https://www.certitudes.org/?wp=" + cid,
            "cacheCode": parent, "publishedAt": published}


class PublicationDiscoveryTests(unittest.TestCase):
    controls = {"countries": {"china": "1", "taiwan": "2", "hong-kong": "3", "macao": "4"},
                "publicationSortUrl": "https://www.certitudes.org/?sort=dat&dir=desc"}

    def run_index(self, page_map, candidates=({"code": "GC1"},), checks=None, limit=100, scans=None):
        for state in (scans or {}).values():
            state.setdefault("candidateFingerprint", discovery._candidate_fingerprint({c["code"] for c in candidates}))
        store, fetcher = Store(scans), Fetcher(page_map)
        parser = lambda html, url, expected_country_id=None: self.controls if html == "controls" else html
        with patch.object(discovery, "parse_dashboard_html", side_effect=parser), \
             patch.object(discovery, "_now", return_value=__import__("datetime").datetime(2026, 11, 2, tzinfo=__import__("datetime").timezone.utc)):
            result = discovery.index_publications(store, fetcher, list(candidates), checks if checks is not None else {},
                                                  page_limit=limit, session_factory=Session)
        return result, store, fetcher

    def test_initial_scan_walks_all_pages_even_when_checker_already_known_and_filters_parent(self):
        cursor = "https://www.certitudes.org/?sort=dat&dir=desc&idx=opaque"
        pages = {"https://www.certitudes.org/": "controls"}
        for country in "1234":
            pages["POST:" + country] = page(country, ([row("KNOWN", published="2026-01-01T00:00:00Z"),
                row("OUT", "GCOTHER", "2025-12-31T00:00:00Z")] if country == "1" else []), cursor if country == "1" else None)
        pages[cursor] = page("1", [row("SECOND", published="2025-12-31T00:00:00Z")])
        known = {"KNOWN": {"checkerId": "KNOWN", "crawl": {"status": "ok"}, "activity": {},
                           "successCount": 1, "failureCount": 0, "counterObservedAt": "2026-01-01T00:00:00Z",
                           "solverListCoverage": "complete", "solverListObservedAt": "2026-01-01T00:00:00Z"}}
        out, store, fetcher = self.run_index(pages, checks=known)
        self.assertEqual(out["pages"], 5)
        self.assertEqual(out["queued"], 1)
        self.assertEqual(out["skipped"], 1)
        self.assertEqual([x for x in fetcher.calls if x[0] == "POST"], [("POST", c) for c in "1234"])
        self.assertEqual([e[1] for e in store.events if e[0] == "checker"], ["KNOWN", "SECOND"])
        self.assertIn(("GET", cursor), fetcher.calls)
        self.assertTrue(all(e[0] == "checker" for e in store.events[:1]))

    def test_page_limit_saves_cursor_after_pending_row_and_resumes_with_frozen_head(self):
        cursor = "https://www.certitudes.org/?sort=dat&dir=desc&idx=saved"
        pages = {"https://www.certitudes.org/": "controls"}
        for c in "1234": pages["POST:" + c] = page(c)
        pages["POST:1"] = page("1", [row("A")], cursor)
        pages[cursor] = page("1", [row("B", published="2025-01-01T00:00:00Z")])
        out, store, _ = self.run_index(pages, limit=1)
        self.assertIn("china", out["partialRegions"])
        self.assertEqual(store.scans["published:china"]["resumeUrl"], cursor)
        self.assertEqual(store.events[0][0], "checker")
        china_events = [e for e in store.events if e[0] == "checker" or e[1] == "published:china"]
        self.assertEqual([e[0] for e in china_events[:2]], ["checker", "scan"])
        self.assertEqual(store.scans["published:china"]["scanNewestPublishedAt"], "2026-11-02T00:00:00Z")

    def test_zero_page_limit_reads_to_terminal_page(self):
        cursor = "https://www.certitudes.org/?sort=dat&dir=desc&idx=older"
        pages = {"https://www.certitudes.org/": "controls"}
        for country in "1234":
            pages["POST:" + country] = page(country)
        pages["POST:1"] = page("1", [row("HEAD")], cursor)
        pages[cursor] = page("1", [row("OLDER", published="2020-01-01T00:00:00Z")])
        result, store, fetcher = self.run_index(pages, limit=0)
        self.assertEqual(result["pages"], 5)
        self.assertEqual(result["failedRegions"], [])
        self.assertEqual(result["partialRegions"], [])
        self.assertIn(("GET", cursor), fetcher.calls)
        self.assertFalse(store.scans["published:china"]["inProgress"])

    def test_negative_page_limit_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "nonnegative"):
            self.run_index({}, limit=-1)

    def test_equal_publication_cutoff_continues_but_strictly_older_page_stops(self):
        cursor = "https://www.certitudes.org/?sort=dat&dir=desc&idx=older"
        pages = {"https://www.certitudes.org/": "controls"}
        for c in "1234": pages["POST:" + c] = page(c)
        pages["POST:1"] = page("1", [row("EQUAL", published="2026-10-01T00:00:00Z")], cursor)
        pages[cursor] = page("1", [row("OLD", published="2026-09-30T23:59:59Z")])
        prior = {"published:china": {"publicationCoveredThroughAt": "2026-10-01T00:00:00Z"}}
        out, _, fetcher = self.run_index(pages, scans=prior)
        self.assertIn(("GET", cursor), fetcher.calls)
        self.assertNotIn("china", out["failedRegions"])

        pages["POST:1"] = page("1", [row("OLDER", published="2026-09-30T23:59:59Z")], cursor)
        out, _, fetcher = self.run_index(pages, scans=prior)
        self.assertNotIn(("GET", cursor), fetcher.calls)
        self.assertNotIn("china", out["failedRegions"])

    def test_bad_order_unknown_timestamp_and_unsafe_cursor_do_not_advance_watermark(self):
        good_cursor = "https://www.certitudes.org/?sort=dat&dir=desc&idx=next"
        pages = {"https://www.certitudes.org/": "controls"}
        for c in "1234": pages["POST:" + c] = page(c)
        pages["POST:1"] = page("1", [row("A", published="2026-11-02T00:00:00Z"),
                                      row("B", published="2026-11-03T00:00:00Z")])
        prior = {"published:china": {"publicationCoveredThroughAt": "2026-10-01T00:00:00Z"}}
        out, store, _ = self.run_index(pages, scans=prior)
        self.assertIn("china", out["failedRegions"])
        self.assertEqual(store.scans["published:china"]["publicationCoveredThroughAt"], "2026-10-01T00:00:00Z")

        pages["POST:1"] = page("1", [row("A", published=None)], good_cursor)
        pages[good_cursor] = page("1", [row("B", published="2025-01-01T00:00:00Z")])
        out, store, _ = self.run_index(pages, scans=prior)
        self.assertIn("china", out["partialRegions"])
        self.assertEqual(store.scans["published:china"]["publicationCoveredThroughAt"], "2026-10-01T00:00:00Z")

        pages["POST:1"] = page("1", [row("A")], "https://example.com/?sort=dat&dir=desc&idx=bad")
        out, store, _ = self.run_index(pages, scans=prior)
        self.assertIn("china", out["failedRegions"])
        self.assertEqual(store.scans["published:china"]["publicationCoveredThroughAt"], "2026-10-01T00:00:00Z")

    def test_resume_rechecks_head_then_cursor_and_keeps_frozen_watermark(self):
        cursor = "https://www.certitudes.org/?sort=dat&dir=desc&idx=saved"
        initial = {"https://www.certitudes.org/": "controls"}
        for c in "1234": initial["POST:" + c] = page(c)
        initial["POST:1"] = page("1", [row("FIRST")], cursor)
        initial[cursor] = page("1", [row("SAVED", published="2025-01-01T00:00:00Z")])
        _, store, _ = self.run_index(initial, limit=1)
        self.assertEqual(store.scans["published:china"]["scanNewestPublishedAt"], "2026-11-02T00:00:00Z")

        resumed = {"https://www.certitudes.org/": "controls", "POST:1": page("1", [row("NEW", published="2026-11-03T00:00:00Z")]),
                   cursor: page("1", [row("SAVED", published="2025-01-01T00:00:00Z")])}
        for c in "234": resumed["POST:" + c] = page(c)
        out, store, fetcher = self.run_index(resumed, limit=2, scans=store.scans)
        self.assertGreaterEqual(out["queued"], 1)
        self.assertIn(("GET", cursor), fetcher.calls)
        self.assertEqual(store.scans["published:china"]["scanNewestPublishedAt"], "2026-11-02T00:00:00Z")
        first_post = next(i for i, call in enumerate(fetcher.calls) if call[0] == "POST")
        cursor_get = fetcher.calls.index(("GET", cursor))
        self.assertLess(first_post, cursor_get)

    def test_cursor_request_failure_keeps_cursor_and_watermark_for_retry(self):
        cursor = "https://www.certitudes.org/?sort=dat&dir=desc&idx=retry"
        prior = {"published:china": {"publicationCoveredThroughAt": "2026-10-01T00:00:00Z",
                    "inProgress": True, "scanNewestPublishedAt": "2026-11-02T00:00:00Z", "resumeUrl": cursor}}
        pages = {"https://www.certitudes.org/": "controls", "POST:1": page("1", [row("HEAD", published="2026-11-03T00:00:00Z")]),
                 cursor: RuntimeError("temporary failure")}
        for c in "234": pages["POST:" + c] = page(c)
        out, store, _ = self.run_index(pages, scans=prior)
        self.assertIn("china", out["failedRegions"])
        self.assertEqual(store.scans["published:china"]["resumeUrl"], cursor)
        self.assertEqual(store.scans["published:china"]["publicationCoveredThroughAt"], "2026-10-01T00:00:00Z")

    def test_complete_checker_is_retained_and_not_written_again(self):
        completed = {"checkerId": "DONE", "sourceUrl": "source", "cacheCode": "GC1", "mappingStatus": "verified",
                     "publishedAt": "2020-01-01T00:00:00Z", "successCount": 3, "failureCount": 2,
                     "counterObservedAt": "2026-01-01T00:00:00Z", "publicSolvers": [{"username": "solver"}],
                     "solverListCoverage": "complete", "solverListObservedAt": "2026-01-01T00:00:00Z",
                     "crawl": {"status": "ok"}, "activity": {"pending": False}}
        pages = {"https://www.certitudes.org/": "controls"}
        for c in "1234": pages["POST:" + c] = page(c, [row("DONE")]) if c == "1" else page(c)
        out, store, _ = self.run_index(pages, checks={"DONE": completed})
        self.assertEqual(out["queued"], 0)
        self.assertEqual(out["skipped"], 1)
        saved = store.saved_checkers[0]
        self.assertEqual(saved["successCount"], 3)
        self.assertEqual(saved["failureCount"], 2)
        self.assertEqual(saved["counterObservedAt"], completed["counterObservedAt"])
        self.assertEqual(saved["solverListObservedAt"], completed["solverListObservedAt"])
        out, second_store, _ = self.run_index(pages, checks={"DONE": saved})
        self.assertEqual(out["queued"], 0)
        self.assertEqual(second_store.saved_checkers, [])

    def test_filter_mismatch_fails_region_and_each_region_gets_anonymous_fresh_session(self):
        Session.created.clear()
        pages = {"https://www.certitudes.org/": "controls"}
        for c in "1234": pages["POST:" + c] = page(c)
        restrictive = page("1"); restrictive["filters"]["filter_user"] = "someone"
        pages["POST:1"] = restrictive
        out, _, fetcher = self.run_index(pages)
        self.assertIn("china", out["failedRegions"])
        region_sessions = [s for s in Session.created if s.calls and s.calls[0][0] == "POST"]
        self.assertEqual(len(region_sessions), 4)
        self.assertEqual(len({id(s) for s in region_sessions}), 4)
        self.assertTrue(all(s.closed for s in Session.created))
        posts = [call for s in region_sessions for call in s.calls if call[0] == "POST"]
        self.assertTrue(all(call[1] == {"filter_owner": "", "filter_user": "", "filter_region": "All",
            "filter_quadrangle": "All", "filter_country": call[1]["filter_country"]}
            for s in region_sessions for call in s.calls if call[0] == "POST"))
        self.assertEqual({call[1]["filter_country"] for s in region_sessions for call in s.calls if call[0] == "POST"},
                         {"1", "2", "3", "4"})

    def test_malformed_control_page_marks_every_region_failed(self):
        store, fetcher = Store(), Fetcher({"https://www.certitudes.org/": "controls"})
        with patch.object(discovery, "parse_dashboard_html", side_effect=ValueError("bad controls")), \
             patch.object(discovery, "_now", return_value=__import__("datetime").datetime(2026, 11, 2, tzinfo=__import__("datetime").timezone.utc)):
            out = discovery.index_publications(store, fetcher, [{"code": "GC1"}], {}, session_factory=Session)
        self.assertEqual(out["failedRegions"], ["china", "taiwan", "hong-kong", "macao"])
        self.assertTrue(all(store.scans["published:" + r]["status"] == "error" for r in discovery.REGIONS))

    def test_new_local_mystery_restarts_discovery_before_old_publication_cutoff(self):
        cursor = "https://www.certitudes.org/?sort=dat&dir=desc&idx=older"
        pages = {"https://www.certitudes.org/": "controls"}
        for country in "1234":
            pages["POST:" + country] = page(country)
        pages["POST:1"] = page("1", [row("HEAD")], cursor)
        pages[cursor] = page("1", [row("OLD", parent="GC2", published="2020-01-01T00:00:00Z")])
        _, store, _ = self.run_index(pages)
        pages["POST:1"] = page("1", [row("OLDER_HEAD", published="2026-11-01T00:00:00Z")], cursor)
        checkers = {}
        out, _, fetcher = self.run_index(pages, candidates=[{"code": "GC1"}, {"code": "GC2"}],
                                        scans=store.scans, checks=checkers)
        self.assertFalse(out["failedRegions"])
        self.assertIn(("GET", cursor), fetcher.calls)
        self.assertIn("OLD", checkers)

if __name__ == "__main__": unittest.main()
