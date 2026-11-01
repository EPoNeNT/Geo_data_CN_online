import sys
import unittest
import json
from unittest.mock import patch
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import certitude_activity as activity
from certitude_activity import activity_window, should_refresh_checker


class ActivityTests(unittest.TestCase):
    def test_persisted_activity_bounds_use_utc_z(self):
        window = activity_window(1, "2026-11-01T00:00:00Z", "2026-11-01T00:00:01Z", {})
        self.assertTrue(window["startAt"].endswith("Z"))
        self.assertTrue(window["endAt"].endswith("Z"))

    def test_age_is_elapsed_seconds_and_stale_cache_is_unverified(self):
        w = activity_window(60, "2026-11-01T00:00:00Z", "2026-11-01T00:01:00Z", {})
        self.assertEqual(w["endAt"], "2026-11-01T00:00:00Z")
        self.assertFalse(w["clockVerified"])

    def test_fresh_response_header_condition(self):
        w = activity_window(5, "2026-11-01T00:00:00Z", "2026-11-01T00:00:01Z",
                            {"Date": "Sun, 01 Nov 2026 00:00:01 GMT", "Cache-Control": "private",
                             "Expires": "Sun, 01 Nov 2026 00:00:01 GMT"})
        self.assertTrue(w["clockVerified"])

    def test_inclusive_detail_boundary_and_unknown_window_refresh(self):
        prev = {"crawl": {"status": "ok", "lastAttemptAt": "2026-11-01T00:00:00Z"},
                "successCount": 1, "failureCount": 0, "counterObservedAt": "2026-11-01T00:00:00Z",
                "solverListCoverage": "complete", "solverListObservedAt": "2026-11-01T00:00:00Z",
                "activity": {}}
        self.assertTrue(should_refresh_checker(prev, {}, {"startAt": None, "endAt": None}))
        prev["activity"]["detailStartedAt"] = "2026-11-01T00:00:00Z"
        self.assertTrue(should_refresh_checker(prev, {}, {"startAt": "x", "endAt": "2026-11-01T00:00:00Z"}))

    def test_unchanged_observations_before_detail_start_skip(self):
        prev = {"crawl": {"status": "ok", "lastAttemptAt": "2026-11-01T00:00:00Z"},
                "successCount": 1, "failureCount": 0, "counterObservedAt": "2026-10-01T00:00:00Z",
                "solverListCoverage": "complete", "solverListObservedAt": "2026-10-01T00:00:00Z",
                "activity": {"detailStartedAt": "2026-11-01T00:00:00Z"}}
        w = {"startAt": "2026-10-01T00:00:00Z", "endAt": "2026-10-02T00:00:00Z"}
        row = {"successCount": 1, "failureCount": 0, "counterObservedAt": "2026-11-01T00:00:00Z"}
        self.assertFalse(should_refresh_checker(prev, row, w))

    def test_invalid_age_has_no_bounds(self):
        w = activity_window(None, "2026-11-01T00:00:00Z", "2026-11-01T00:01:00Z", {})
        self.assertIsNone(w["startAt"])
        self.assertIsNone(w["endAt"])
        self.assertEqual(w["referenceStartAt"], "2026-11-01T00:00:00Z")

    def test_age_header_marks_cached_response_unverified(self):
        w = activity_window(1, "2026-11-01T00:00:00Z", "2026-11-01T00:00:01Z",
                            {"Date": "Sun, 01 Nov 2026 00:00:01 GMT", "Age": "4",
                             "Cache-Control": "private", "Expires": "Sun, 01 Nov 2026 00:00:01 GMT"})
        self.assertFalse(w["clockVerified"])

    def test_no_store_can_verify_without_expires(self):
        w = activity_window(1, "2026-11-01T00:00:00Z", "2026-11-01T00:00:01Z",
                            {"Date": "Sun, 01 Nov 2026 00:00:01 GMT", "Cache-Control": "private, no-store"})
        self.assertTrue(w["clockVerified"])

    def test_initial_error_pending_and_counter_change_retry(self):
        base = {"crawl": {"status": "ok", "lastAttemptAt": "2026-11-01T00:00:00Z"},
                "successCount": 1, "failureCount": 0, "counterObservedAt": "2026-10-01T00:00:00Z",
                "solverListCoverage": "complete", "solverListObservedAt": "2026-10-01T00:00:00Z",
                "activity": {"detailStartedAt": "2026-11-01T00:00:00Z"}}
        w = {"startAt": "2026-10-01T00:00:00Z", "endAt": "2026-10-02T00:00:00Z"}
        row = {"successCount": 1, "failureCount": 0, "counterObservedAt": "2026-11-01T00:00:00Z"}
        self.assertTrue(should_refresh_checker(None, row, w))
        retry = {**base, "crawl": {"status": "error"}}
        self.assertTrue(should_refresh_checker(retry, row, w))
        retry = {**base, "activity": {"pending": True}}
        self.assertTrue(should_refresh_checker(retry, row, w))
        changed = {**row, "successCount": 2}
        self.assertTrue(should_refresh_checker(base, changed, w))

    def test_incomplete_pair_forces_refresh(self):
        prev = {"crawl": {"status": "ok", "lastAttemptAt": "2026-11-01T00:00:00Z"},
                "successCount": 1, "failureCount": None, "counterObservedAt": "2026-10-01T00:00:00Z",
                "solverListCoverage": "complete", "solverListObservedAt": "2026-10-01T00:00:00Z",
                "activity": {"detailStartedAt": "2026-11-01T00:00:00Z"}}
        row = {"successCount": 1, "failureCount": 0, "counterObservedAt": "2026-11-01T00:00:00Z"}
        self.assertTrue(should_refresh_checker(prev, row, {"startAt": "2026-10-01T00:00:00Z", "endAt": "2026-10-02T00:00:00Z"}))

    def test_dashboard_counter_values_without_observation_timestamp_can_skip_old_activity(self):
        prev = {"crawl": {"status": "ok", "lastAttemptAt": "2026-11-01T00:00:00Z"},
                "successCount": 1, "failureCount": 0, "counterObservedAt": "2026-10-01T00:00:00Z",
                "solverListCoverage": "complete", "solverListObservedAt": "2026-10-01T00:00:00Z",
                "activity": {"detailStartedAt": "2026-11-01T00:00:00Z"}}
        row = {"successCount": 1, "failureCount": 0}
        w = {"startAt": "2026-10-01T00:00:00Z", "endAt": "2026-10-02T00:00:00Z"}
        self.assertFalse(should_refresh_checker(prev, row, w))


class FakeSession:
    def __init__(self): self.headers = {}; self.closed = False
    def close(self): self.closed = True


class FakeFetcher:
    def __init__(self, pages):
        self.pages = dict(pages)
        self.last_response_headers = {"Date": "Sun, 01 Nov 2026 00:00:00 GMT",
                                      "Cache-Control": "private",
                                      "Expires": "Sun, 01 Nov 2026 00:00:00 GMT"}
        self.calls = []
    def get(self, session, url, hosts):
        self.calls.append(("GET", url))
        value = self.pages.get(url)
        if isinstance(value, BaseException): raise value
        if value is None: raise ValueError("fixture page unavailable")
        return value if isinstance(value, str) else json.dumps(value), url
    def post(self, session, url, hosts, data):
        self.calls.append(("POST", url, dict(data)))
        value = self.pages.get("POST:" + data["filter_country"])
        if isinstance(value, BaseException): raise value
        if value is None: raise ValueError("fixture page unavailable")
        return value if isinstance(value, str) else json.dumps(value), "https://www.certitudes.org/"


class FakeStore:
    def __init__(self, scans=None): self.scans = scans or {}; self.events = []
    def load_activity_scans(self): return self.scans
    def save_checker(self, rec): self.events.append(("checker", rec["checkerId"]))
    def save_activity_scan(self, scope, record):
        self.events.append(("scan", scope, dict(record)))
        self.scans[scope] = dict(record)


class ActivityIndexTests(unittest.TestCase):
    now = datetime(2026, 11, 1, tzinfo=timezone.utc)
    controls = {"countries": {"china": "1", "taiwan": "2", "hong-kong": "3", "macao": "4"},
                "activitySortUrl": "https://www.certitudes.org/?sort=act&dir=desc",
                "sort": "act", "direction": "desc"}

    @staticmethod
    def page(country="1", rows=(), next_url=None):
        return {"selectedCountry": country, "filters": {"filter_country": country, "filter_region": "All",
                "filter_quadrangle": "All", "filter_owner": "", "filter_user": ""},
                "sort": "act", "direction": "desc", "rows": list(rows), "nextUrl": next_url}

    @staticmethod
    def row(cid, age=10, parent="GC1", success=1):
        return {"checkerId": cid, "sourceUrl": "https://www.certitudes.org/certify/" + cid,
                "cacheCode": parent, "ageSeconds": age, "successCount": success, "failureCount": 0}

    def run_index(self, pages, candidates=None, scans=None, limit=100, times=None):
        store, fetcher = FakeStore(scans), FakeFetcher(pages)
        parser = lambda marker, url, expected_country_id=None: (
            self.controls if marker == "controls" else
            pages[marker] if marker in pages and isinstance(pages[marker], dict) else json.loads(marker))
        ticks = iter(times or [self.now] * 20)
        with patch.object(activity, "parse_dashboard_html", side_effect=parser), \
                patch.object(activity, "_now", side_effect=lambda: next(ticks)):
            out = activity.index_activity(store, fetcher, candidates or [{"code": "GC1", "country": "China"}], {},
                                          page_limit=limit, session_factory=FakeSession)
        return out, store, fetcher

    def test_initial_scan_enumerates_both_pages_and_saves_rows_before_checkpoint(self):
        next_url = "https://www.certitudes.org/?sort=act&dir=desc&idx=opaque"
        pages = {"https://www.certitudes.org/": "controls", "POST:1": "region",
                 "region": self.page(rows=[self.row("A")], next_url=next_url),
                 next_url: self.page(rows=[self.row("B", age=20)])}
        out, store, fetcher = self.run_index(pages)
        self.assertEqual(out["pages"], 2)
        self.assertEqual(out["queued"], 2)
        self.assertEqual([e[0] for e in store.events], ["checker", "scan", "checker", "scan"])
        self.assertFalse(store.events[-1][2]["inProgress"])
        self.assertIn(("GET", next_url), fetcher.calls)

    def test_equal_old_cutoff_boundary_includes_next_page(self):
        next_url = "https://www.certitudes.org/?sort=act&dir=desc&idx=two"
        old_cutoff = "2026-11-01T00:00:00+00:00"
        scans = {"regional:china": {"indexCoveredThroughAt": old_cutoff}}
        # Activity end equals cutoff because age is zero and deterministic clock is fixed.
        pages = {"https://www.certitudes.org/": "controls", "POST:1": "region",
                 "region": self.page(rows=[self.row("A", age=0)], next_url=next_url),
                 next_url: self.page(rows=[self.row("B", age=0)])}
        out, _, fetcher = self.run_index(pages, scans=scans)
        self.assertIn(("GET", next_url), fetcher.calls)
        self.assertEqual(out["queued"], 2)

    def test_limit_one_resume_processes_fresh_head_then_cursor_without_starvation(self):
        cursor = "https://www.certitudes.org/?sort=act&dir=desc&idx=saved"
        scans = {"regional:china": {"indexCoveredThroughAt": "2026-10-01T00:00:00+00:00",
                    "inProgress": True, "scanStartedAt": "2026-10-31T00:00:00+00:00", "resumeUrl": cursor}}
        pages = {"https://www.certitudes.org/": "controls", "POST:1": "region",
                 "region": self.page(rows=[self.row("HEAD")]),
                 cursor: self.page(rows=[self.row("CURSOR")], next_url="https://www.certitudes.org/?sort=act&dir=desc&idx=next")}
        out, store, fetcher = self.run_index(pages, scans=scans, limit=1)
        self.assertEqual(out["queued"], 2)
        self.assertIn(("GET", cursor), fetcher.calls)
        state = store.scans["regional:china"]
        self.assertTrue(state["inProgress"])
        self.assertEqual(state["resumeUrl"], "https://www.certitudes.org/?sort=act&dir=desc&idx=next")

    def test_cursor_error_keeps_old_watermark_and_cursor(self):
        cursor = "https://www.certitudes.org/?sort=act&dir=desc&idx=saved"
        scans = {"regional:china": {"indexCoveredThroughAt": "2026-10-01T00:00:00+00:00",
                    "inProgress": True, "scanStartedAt": "2026-10-31T00:00:00+00:00", "resumeUrl": cursor}}
        pages = {"https://www.certitudes.org/": "controls", "POST:1": "region",
                 "region": self.page(rows=[self.row("HEAD")]), cursor: "bad"}
        out, store, _ = self.run_index(pages, scans=scans, limit=2)
        self.assertEqual(out["failedRegions"], ["china"])
        saved = store.scans["regional:china"]
        self.assertEqual(saved["indexCoveredThroughAt"], scans["regional:china"]["indexCoveredThroughAt"])
        self.assertEqual(saved["resumeUrl"], cursor)

    def test_unknown_country_candidate_admits_parent_and_ignores_outside_and_parentless_rows(self):
        pages = {"https://www.certitudes.org/": "controls", "POST:1": "region",
                 "region": self.page(rows=[self.row("IN", parent="GC9"), self.row("OUT", parent="GCX"),
                                            self.row("NONE", parent=None)])}
        out, store, _ = self.run_index(pages, candidates=[{"code": "GC9", "country": "Other"}])
        ids = [e[1] for e in store.events if e[0] == "checker"]
        self.assertEqual(ids, ["IN"])
        self.assertEqual(out["queued"], 1)

    def test_unverified_cache_clock_cannot_advance_watermark(self):
        pages = {"https://www.certitudes.org/": "controls", "POST:1": "region",
                 "region": self.page(rows=[self.row("A")])}
        store, fetcher = FakeStore(), FakeFetcher(pages)
        fetcher.last_response_headers = {"Date": "Sun, 01 Nov 2026 00:00:00 GMT", "Age": "5",
                                         "Cache-Control": "private", "Expires": "Sun, 01 Nov 2026 00:00:00 GMT"}
        parser = lambda marker, url, expected_country_id=None: (
            self.controls if marker == "controls" else
            pages[marker] if marker in pages and isinstance(pages[marker], dict) else json.loads(marker))
        with patch.object(activity, "parse_dashboard_html", side_effect=parser), patch.object(activity, "_now", return_value=self.now):
            out = activity.index_activity(store, fetcher, [{"code": "GC1", "country": "China"}], {}, session_factory=FakeSession)
        self.assertIn("china", out["partialRegions"])
        self.assertIsNone(store.scans["regional:china"]["indexCoveredThroughAt"])

    def test_page_filter_sort_mismatch_is_rejected(self):
        bad = self.page(rows=[self.row("A")]); bad["direction"] = "asc"
        pages = {"https://www.certitudes.org/": "controls", "POST:1": "region", "region": bad}
        out, store, _ = self.run_index(pages)
        self.assertEqual(out["failedRegions"], ["china"])
        self.assertEqual(store.scans["regional:china"]["status"], "error")
        self.assertIsNone(store.scans["regional:china"]["indexCoveredThroughAt"])

    def test_control_fetch_failure_records_error_without_advancing_old_watermark(self):
        prior = {"indexCoveredThroughAt": "2026-10-01T00:00:00+00:00", "status": "ok"}
        pages = {"https://www.certitudes.org/": RuntimeError("offline")}
        out, store, _ = self.run_index(pages, scans={"regional:china": prior})
        self.assertEqual(out["failedRegions"], ["china"])
        self.assertEqual(store.scans["regional:china"]["indexCoveredThroughAt"], prior["indexCoveredThroughAt"])
        self.assertEqual(store.scans["regional:china"]["status"], "error")


if __name__ == "__main__":
    unittest.main()
