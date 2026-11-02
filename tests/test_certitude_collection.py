import copy
import json
import shutil
import sys
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import crawl_certitude
from certitude_source import parse_checker_html
from certitude_store import merge_checker
from generate_certitude import build_dataset, publish_dataset


class FakeConnection:
    def __init__(self):
        self.state = {"descriptions": {}, "checkers": {}, "snapshots": [], "activity_scans": {}, "usage_audits": {}}


class FakeStore:
    def __init__(self, connection):
        self.state = connection.state

    def ensure_schema(self):
        pass

    def load_descriptions(self):
        return copy.deepcopy(self.state["descriptions"])

    def load_checkers(self):
        return copy.deepcopy(self.state["checkers"])

    def load_usage_audits(self):
        return copy.deepcopy(self.state["usage_audits"])

    def save_usage_audit(self, code, record):
        self.state["usage_audits"][code] = copy.deepcopy(record)

    def load_activity_scans(self):
        return copy.deepcopy(self.state["activity_scans"])

    def save_activity_scan(self, scope, record):
        self.state["activity_scans"][scope] = copy.deepcopy(record)

    def save_description(self, code, record):
        self.state["descriptions"][code] = copy.deepcopy(record)

    def save_checker(self, record, snapshot=None):
        self.state["checkers"][record["checkerId"]] = copy.deepcopy(record)
        if snapshot is not None:
            self.state["snapshots"].append(copy.deepcopy(snapshot))


class FakeSession:
    def __init__(self):
        self.headers = {}

    def close(self):
        pass


class FakeFetcher:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def get(self, session, url, allowed_hosts):
        self.calls.append(url)
        response = self.responses.get(url, self.responses.get("default"))
        if isinstance(response, Exception):
            raise response
        if response is None:
            raise crawl_certitude.FetchError("not_found")
        return response, url


def cache(code="GC12345", *, archived=True, premium=False):
    return {
        "code": code, "name": "Mystery", "owner_username": "Owner", "country": "China",
        "city": "Beijing", "cache_status": 2 if archived else 1, "difficulty": 2.0,
        "terrain": 2.0, "placed_date": "2020-01-01", "details_url": "https://coord.info/" + code,
        "premium_only": premium, "logs_crawled_at": None, "source_version": "source-1",
    }



def checker_html(checker_id="GC12345", parent="GC12345", success=4, failure=2, solvers="[]"):
    return f'''<h1><a href="?wp={checker_id}">{checker_id}</a>
      <a href="https://coord.info/{parent}">Cache</a></h1>
      <b class="success">{success}</b><b class="error">{failure}</b>
      <table><tbody id="solvers"></tbody></table>
      <script src="https://static.certitudes.org/html/certitude.js"></script>
      <script>setTimezone("Europe/Paris"); showTimeline("en-US", 1600000000, 1700000000);
      var solvers = {solvers}; showSolvers("", "en-US", solvers);</script>'''


class CertitudeCollectionIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.patches = [
            patch.object(crawl_certitude, "CertitudeStore", FakeStore),
            patch.object(crawl_certitude, "load_candidates", return_value=[cache(), cache("GC54321")]),
            patch.object(crawl_certitude.requests, "Session", return_value=FakeSession()),
            patch.object(crawl_certitude, "index_publications", return_value={
                "pages": 0, "queued": 0, "skipped": 0, "failedRegions": [], "partialRegions": [],
            }),
            patch.object(crawl_certitude, "index_activity", return_value={
                "pages": 0, "queued": 0, "skipped": 0, "failedRegions": [], "partialRegions": [],
            }),
        ]
        for active in self.patches:
            active.start()
            self.addCleanup(active.stop)

    def collect(self, conn, fetcher, *, activity_limit=100, publication_limit=100, logger=None):
        return crawl_certitude.collect(conn, activity_page_limit=activity_limit,
                                       publication_page_limit=publication_limit,
                                       fetcher=fetcher, logger=logger)

    def test_default_discovery_does_not_load_descriptions_or_use_geocaching_session(self):
        conn = FakeConnection()
        crawl_certitude.load_candidates = lambda _conn: [cache()]
        with patch.object(FakeStore, "load_descriptions", side_effect=AssertionError("description read")), \
                patch.object(FakeStore, "save_description", side_effect=AssertionError("description write")):
            crawl_certitude.collect(conn, fetcher=FakeFetcher({}))

    def test_publication_discovery_supplies_detail_without_cookie_or_description(self):
        conn = FakeConnection()
        crawl_certitude.load_candidates = lambda _conn: [cache()]
        url = "https://www.certitudes.org/certitude?wp=GC12345"

        def discover(store, fetcher, candidates, checkers, **kwargs):
            pending = {"checkerId": "GC12345", "sourceUrl": url, "mappingStatus": "pending",
                       "activity": {"pending": True, "expectedCacheCode": "GC12345"},
                       "crawl": {"status": "not_collected"}}
            store.save_checker(pending)
            checkers["GC12345"] = pending
            return {"pages": 4, "queued": 1, "skipped": 0, "failedRegions": [], "partialRegions": []}

        with patch.object(crawl_certitude, "index_publications", side_effect=discover):
            fetcher = FakeFetcher({url: checker_html()})
            crawl_certitude.collect(conn, fetcher=fetcher)
        saved = conn.state["checkers"]["GC12345"]
        self.assertEqual(saved["mappingStatus"], "verified")
        self.assertEqual(saved["cacheCode"], "GC12345")
        self.assertFalse(saved["activity"]["pending"])
        self.assertEqual(fetcher.calls, [url])
        self.assertEqual(conn.state["descriptions"], {})

    def test_stored_descriptions_are_preserved_without_read_or_write(self):
        conn = FakeConnection()
        original = {"legacy": {"descriptionLinks": [{"sourceUrl": "https://www.certitudes.org/old"}]}}
        conn.state["descriptions"] = copy.deepcopy(original)
        with patch.object(FakeStore, "load_descriptions", side_effect=AssertionError("description read")), \
                patch.object(FakeStore, "save_description", side_effect=AssertionError("description write")):
            self.collect(conn, FakeFetcher({}))
        self.assertEqual(conn.state["descriptions"], original)

    def test_audit_discovered_link_is_verified_once_and_keeps_description_time(self):
        conn = FakeConnection()
        source = "https://www.certitudes.org/certitude?wp=GC12345"
        observed = "2026-11-01T00:00:00Z"
        conn.state["usage_audits"]["GC12345"] = {
            "lastValidStatus": "linked", "checkedAt": observed, "links": [{"sourceUrl": source}],
        }
        fetcher = FakeFetcher({source: checker_html()})
        self.collect(conn, fetcher)
        self.assertEqual(fetcher.calls, [source])
        self.assertEqual(conn.state["checkers"]["GC12345"]["mappingStatus"], "verified")
        audit = conn.state["usage_audits"]["GC12345"]
        self.assertEqual(audit["links"][0]["checkerId"], "GC12345")
        self.assertEqual(audit["checkedAt"], observed)
        self.collect(conn, fetcher)
        self.assertEqual(fetcher.calls, [source])

    def test_failed_audit_link_keeps_last_valid_evidence_for_next_retry(self):
        conn = FakeConnection()
        source = "https://www.certitudes.org/certitude?wp=GC12345"
        observed = "2026-11-01T00:00:00Z"
        conn.state["usage_audits"]["GC12345"] = {
            "lastValidStatus": "linked", "checkedAt": observed, "links": [{"sourceUrl": source}],
        }
        with self.assertRaisesRegex(RuntimeError, "no_valid_checker_observation"):
            self.collect(conn, FakeFetcher({source: crawl_certitude.FetchError("http_500")}))
        audit = conn.state["usage_audits"]["GC12345"]
        self.assertEqual(audit["checkedAt"], observed)
        self.assertEqual(audit["lastValidStatus"], "linked")
        self.assertEqual(audit["links"][0]["errorCode"], "http_500")
        self.assertNotIn("checkerId", audit["links"][0])

    def test_saved_checker_outside_current_mystery_set_is_retained_without_detail_read(self):
        conn = FakeConnection()
        url = "https://www.certitudes.org/certitude?wp=OUT"
        old = {"checkerId": "OUT", "cacheCode": "GCOUT", "sourceUrl": url,
               "mappingStatus": "verified", "crawl": {"status": "error"}}
        conn.state["checkers"]["OUT"] = copy.deepcopy(old)
        fetcher = FakeFetcher({url: crawl_certitude.FetchError("http_500")})
        self.collect(conn, fetcher)
        self.assertEqual(fetcher.calls, [])
        self.assertEqual(conn.state["checkers"]["OUT"], old)

    def test_detail_progress_counts_unique_source_urls(self):
        conn = FakeConnection()
        url_a = "https://www.certitudes.org/a-primary"
        url_b = "https://www.certitudes.org/b-alias"
        for cid, url in (("GC12345", url_a), ("GC54321", url_b)):
            conn.state["checkers"][cid] = {
                "checkerId": cid, "sourceUrl": url,
                "cacheCode": cid, "mappingStatus": "verified",
                "crawl": {"status": "error"},
            }
        fresh = {"checkerId": "GC12345", "cacheCode": "GC12345", "successCount": 1,
                 "failureCount": 0, "counterObservedAt": "2026-11-01T00:00:00Z",
                 "solverListCoverage": "complete", "solverListObservedAt": "2026-11-01T00:00:00Z",
                 "publicSolvers": [], "crawl": {"status": "ok"}}
        messages = []
        class Logger:
            def info(self, message, *args): messages.append(message % args)
            def warning(self, message, *args): messages.append(message % args)
        parsed = iter([fresh, {**fresh, "checkerId": "GC54321", "cacheCode": "GC54321"}])
        with patch.object(crawl_certitude, "parse_checker_html", side_effect=lambda *_args: next(parsed)):
            self.collect(conn, FakeFetcher({url_a: "page", url_b: "page"}), logger=Logger())
        progress = [line for line in messages if "detail progress" in line]
        self.assertTrue(progress, messages)
        self.assertEqual(progress[-1], "Certitude detail progress: processed=2 successful=2 failed=0 skipped_aliases=0")

    def test_unchanged_verified_checker_is_not_refreshed_without_activity_index_hit(self):
        conn = FakeConnection()
        row = cache(archived=False)
        crawl_certitude.load_candidates = lambda _conn: [row]
        old = parse_checker_html(checker_html(), "https://www.certitudes.org/certitude?wp=GC12345",
                                 datetime(2026, 10, 31, tzinfo=timezone.utc))
        old["sourceUrl"] = "https://www.certitudes.org/certitude?wp=GC12345"
        old["successCount"], old["failureCount"] = 1, 1
        old["counterObservedAt"] = "2026-10-31T00:00:00Z"
        old["latestActivityAt"] = "2025-01-01T00:00:00Z"
        old["crawl"] = {"status": "ok", "lastAttemptAt": "2026-10-31T00:00:00Z"}
        conn.state["checkers"][old["checkerId"]] = old
        conn.state["descriptions"]["GC12345"] = {
            "descriptionScannedAt": crawl_certitude.utc_now(), "descriptionScanStatus": "ok",
            "descriptionScanCoverage": "complete",
        }
        fresh = checker_html(success=0, failure=0)
        fetcher = FakeFetcher({old["sourceUrl"]: fresh})
        parser_patch = patch.object(crawl_certitude, "parse_checker_html",
                                    return_value={**old, "successCount": 0, "failureCount": 0,
                                                  "counterObservedAt": "2026-11-01T00:00:00Z",
                                                  "crawl": {"status": "ok", "lastAttemptAt": "2026-11-01T00:00:00Z",
                                                            "lastSuccessAt": "2026-11-01T00:00:00Z",
                                                            "fieldCoverage": "complete"}})
        with parser_patch:
            self.collect(conn, fetcher)
        self.assertEqual(fetcher.calls, [])
        saved = conn.state["checkers"][old["checkerId"]]
        self.assertEqual(saved["successCount"], 1)
        self.assertEqual(saved["latestActivityAt"], old["latestActivityAt"])
        self.assertEqual(saved["crawl"]["lastAttemptAt"], old["crawl"]["lastAttemptAt"])
        self.assertEqual(conn.state["snapshots"], [])

    def test_checker_fetch_error_retains_pair_list_and_emits_no_snapshot(self):
        conn = FakeConnection()
        old = parse_checker_html(checker_html(), "https://www.certitudes.org/certitude?wp=GC12345",
                                 datetime(2026, 10, 31, tzinfo=timezone.utc))
        old.update(sourceUrl="https://www.certitudes.org/certitude?wp=GC12345",
                   successCount=7, failureCount=3, counterObservedAt="2026-10-31T00:00:00Z",
                   publicSolvers=[{"username": "Ada"}], solverListCoverage="complete",
                   solverListObservedAt="2026-10-31T00:00:00Z")
        old["activity"] = {"pending": True}
        conn.state["checkers"]["GC12345"] = old
        fetcher = FakeFetcher({old["sourceUrl"]: crawl_certitude.FetchError("http_500")})
        self.collect(conn, fetcher)
        saved = conn.state["checkers"]["GC12345"]
        self.assertEqual((saved["successCount"], saved["failureCount"]), (7, 3))
        self.assertEqual(saved["counterObservedAt"], old["counterObservedAt"])
        self.assertEqual(saved["publicSolvers"], old["publicSolvers"])
        self.assertEqual(saved["solverListObservedAt"], old["solverListObservedAt"])
        self.assertEqual(saved["crawl"]["status"], "error")
        self.assertTrue(saved["activity"]["pending"])
        self.assertEqual(conn.state["snapshots"], [])

    def test_new_valid_counter_pair_emits_one_snapshot(self):
        conn = FakeConnection()
        old = {"checkerId": "GC12345", "sourceUrl": "https://www.certitudes.org/certitude?wp=GC12345",
               "cacheCode": "GC12345", "mappingStatus": "verified", "role": "final",
               "successCount": 1, "failureCount": 2, "counterObservedAt": "2026-10-31T00:00:00Z",
               "crawl": {"status": "ok"}}
        conn.state["checkers"]["GC12345"] = old
        fresh = {**old, "successCount": 0, "failureCount": 0,
                 "counterObservedAt": "2026-11-01T00:00:00Z",
                 "publicSolvers": [{"username": "NewSolver"}], "solverRowsRetrieved": 1,
                 "solverListCoverage": "complete", "solverListObservedAt": "2026-11-01T00:01:00Z",
                 "latestActivityAt": "2026-10-31T23:59:00Z",
                 "crawl": {"status": "ok", "lastAttemptAt": "2026-11-01T00:00:00Z",
                           "lastSuccessAt": "2026-11-01T00:00:00Z"}}
        with patch.object(crawl_certitude, "parse_checker_html", return_value=fresh):
            with patch.object(crawl_certitude, "index_activity", side_effect=lambda _store, _fetcher, _candidates, checkers, **_kwargs: (
                    checkers["GC12345"].setdefault("activity", {}).update(pending=True) or
                    {"pages": 1, "queued": 1, "skipped": 0, "failedRegions": [], "partialRegions": []})):
                self.collect(conn, FakeFetcher({old["sourceUrl"]: "html"}))
        self.assertEqual(len(conn.state["snapshots"]), 1)
        self.assertEqual(conn.state["snapshots"][0]["successCount"], 0)
        self.assertEqual(conn.state["snapshots"][0]["observedAt"], fresh["counterObservedAt"])
        self.assertEqual(conn.state["snapshots"][0]["latestActivityAt"], fresh["latestActivityAt"])
        saved = conn.state["checkers"]["GC12345"]
        self.assertFalse(saved["activity"]["pending"])
        self.assertEqual(saved["activity"]["detailStartedAt"], saved["crawl"]["lastAttemptAt"])
        self.assertEqual(saved["activity"]["detailObservedAt"], fresh["solverListObservedAt"])

    def test_pending_retry_survives_failure_and_uses_fresh_complete_list_to_clear(self):
        conn = FakeConnection()
        old = {"checkerId": "GC12345", "sourceUrl": "https://www.certitudes.org/certitude?wp=GC12345",
               "cacheCode": "GC12345", "mappingStatus": "verified", "role": "final",
               "successCount": 7, "failureCount": 3, "counterObservedAt": "2026-10-31T00:00:00Z",
               "publicSolvers": [{"username": "Ada"}], "solverListCoverage": "complete",
               "solverListObservedAt": "2026-10-30T00:00:00Z", "latestActivityAt": "2026-10-29T00:00:00Z",
               "activity": {"pending": True}, "crawl": {"status": "ok", "lastAttemptAt": "2026-10-31T00:00:00Z"}}
        conn.state["checkers"]["GC12345"] = old
        url = old["sourceUrl"]
        self.collect(conn, FakeFetcher({url: crawl_certitude.FetchError("http_500")}))
        failed = conn.state["checkers"]["GC12345"]
        self.assertTrue(failed["activity"]["pending"])
        self.assertEqual((failed["successCount"], failed["failureCount"]), (7, 3))
        self.assertEqual(failed["publicSolvers"], old["publicSolvers"])
        self.assertEqual(failed["solverListObservedAt"], old["solverListObservedAt"])
        self.assertEqual(failed["latestActivityAt"], old["latestActivityAt"])
        self.assertEqual(conn.state["snapshots"], [])

        fresh = {**old, "successCount": 8, "failureCount": 3,
                 "counterObservedAt": "2026-11-01T01:00:00Z", "publicSolvers": [{"username": "Grace"}],
                 "solverListCoverage": "complete", "solverListObservedAt": "2026-11-01T01:01:00Z",
                 "latestActivityAt": "2026-11-01T00:59:00Z",
                 "crawl": {"status": "ok", "lastAttemptAt": "2026-11-01T01:00:00Z",
                           "lastSuccessAt": "2026-11-01T01:00:00Z"}}
        with patch.object(crawl_certitude, "parse_checker_html", return_value=fresh):
            self.collect(conn, FakeFetcher({url: "fresh html"}))
        self.assertFalse(conn.state["checkers"]["GC12345"]["activity"]["pending"])
        self.assertEqual(conn.state["checkers"]["GC12345"]["publicSolvers"], [{"username": "Grace"}])
        self.assertEqual(len(conn.state["snapshots"]), 1)

    def test_fresh_counters_with_unknown_solver_list_keep_pending_true(self):
        conn = FakeConnection()
        old = {"checkerId": "GC12345", "sourceUrl": "https://www.certitudes.org/certitude?wp=GC12345",
               "cacheCode": "GC12345", "mappingStatus": "verified", "role": "final",
               "successCount": 7, "failureCount": 3, "counterObservedAt": "2026-10-31T00:00:00Z",
               "publicSolvers": [{"username": "Ada"}], "solverListCoverage": "complete",
               "solverListObservedAt": "2026-10-30T00:00:00Z", "activity": {"pending": True},
               "crawl": {"status": "ok"}}
        conn.state["checkers"]["GC12345"] = old
        fresh = {**old, "successCount": 8, "failureCount": 3,
                 "counterObservedAt": "2026-11-01T02:00:00Z", "publicSolvers": [],
                 "solverListCoverage": "unknown", "solverListObservedAt": None,
                 "crawl": {"status": "ok", "lastAttemptAt": "2026-11-01T02:00:00Z",
                           "lastSuccessAt": "2026-11-01T02:00:00Z"}}
        with patch.object(crawl_certitude, "parse_checker_html", return_value=fresh):
            self.collect(conn, FakeFetcher({old["sourceUrl"]: "fresh html"}))
        saved = conn.state["checkers"]["GC12345"]
        self.assertTrue(saved["activity"]["pending"])
        self.assertEqual(saved["publicSolvers"], old["publicSolvers"])
        self.assertEqual(saved["solverListObservedAt"], old["solverListObservedAt"])

    def test_unmapped_pending_checker_is_retained_after_detail_failure(self):
        conn = FakeConnection()
        url = "https://www.certitudes.org/certitude?wp=GCUNKNOWN"
        pending = {"checkerId": "GCUNKNOWN", "sourceUrl": url, "cacheCode": None,
                   "mappingStatus": "pending", "activity": {"pending": True},
                   "crawl": {"status": "not_collected"}}

        def discover(store, fetcher, candidates, checkers, **kwargs):
            store.save_checker(pending)
            checkers["GCUNKNOWN"] = copy.deepcopy(pending)
            return {"pages": 1, "queued": 1, "skipped": 0,
                    "failedRegions": [], "partialRegions": []}

        fetcher = FakeFetcher({url: crawl_certitude.FetchError("http_500")})
        with patch.object(crawl_certitude, "index_publications", side_effect=discover):
            with self.assertRaisesRegex(RuntimeError, "no_valid_checker_observation"):
                self.collect(conn, fetcher)
        saved = conn.state["checkers"]["GCUNKNOWN"]
        self.assertIsNone(saved["cacheCode"])
        self.assertEqual(saved["mappingStatus"], "pending")
        self.assertTrue(saved["activity"]["pending"])
        self.assertEqual(saved["crawl"]["status"], "error")
        self.assertEqual(saved["crawl"]["errorCode"], "http_500")

    def test_failed_dashboard_region_raises_without_fetching_unchanged_details(self):
        conn = FakeConnection()
        old = {"checkerId": "GC12345", "sourceUrl": "https://www.certitudes.org/certitude?wp=GC12345",
               "successCount": 7, "failureCount": 3, "counterObservedAt": "2026-10-31T00:00:00Z",
               "publicSolvers": [], "solverListCoverage": "complete",
               "solverListObservedAt": "2026-10-31T00:00:00Z", "crawl": {"status": "ok"}}
        conn.state["checkers"]["GC12345"] = old
        fetcher = FakeFetcher({})
        with patch.object(crawl_certitude, "index_activity", return_value={
                "pages": 0, "queued": 0, "skipped": 0, "failedRegions": ["china"], "partialRegions": []}):
            with self.assertRaisesRegex(RuntimeError, "dashboard_scan_failed"):
                self.collect(conn, fetcher)
        self.assertEqual(fetcher.calls, [])

    def test_failed_dashboard_region_still_processes_existing_pending_details(self):
        conn = FakeConnection()
        url = "https://www.certitudes.org/certitude?wp=GC12345"
        old = {"checkerId": "GC12345", "sourceUrl": url, "cacheCode": "GC12345",
               "mappingStatus": "verified", "successCount": 7, "failureCount": 3,
               "counterObservedAt": "2026-10-31T00:00:00Z", "publicSolvers": [],
               "solverListCoverage": "complete", "solverListObservedAt": "2026-10-31T00:00:00Z",
               "activity": {"pending": True}, "crawl": {"status": "ok"}}
        conn.state["checkers"]["GC12345"] = old
        fresh = {**old, "successCount": 8, "counterObservedAt": "2026-11-01T03:00:00Z",
                 "solverListObservedAt": "2026-11-01T03:01:00Z",
                 "crawl": {"status": "ok", "lastAttemptAt": "2026-11-01T03:00:00Z",
                           "lastSuccessAt": "2026-11-01T03:00:00Z"}}
        fetcher = FakeFetcher({url: "fresh html"})
        with patch.object(crawl_certitude, "parse_checker_html", return_value=fresh):
            with patch.object(crawl_certitude, "index_activity", return_value={
                    "pages": 0, "queued": 0, "skipped": 0, "failedRegions": ["china"], "partialRegions": []}):
                with self.assertRaisesRegex(RuntimeError, "dashboard_scan_failed"):
                    self.collect(conn, fetcher)
        self.assertEqual(fetcher.calls, [url])
        self.assertFalse(conn.state["checkers"]["GC12345"]["activity"]["pending"])
        self.assertEqual(len(conn.state["snapshots"]), 1)

    def test_failed_publication_scan_still_processes_existing_pending_details(self):
        conn = FakeConnection()
        url = "https://www.certitudes.org/certitude?wp=GC12345"
        old = {"checkerId": "GC12345", "sourceUrl": url, "cacheCode": "GC12345",
               "mappingStatus": "verified", "successCount": 7, "failureCount": 3,
               "counterObservedAt": "2026-10-31T00:00:00Z", "publicSolvers": [],
               "solverListCoverage": "complete", "solverListObservedAt": "2026-10-31T00:00:00Z",
               "activity": {"pending": True}, "crawl": {"status": "ok"}}
        conn.state["checkers"]["GC12345"] = old
        fresh = {**old, "successCount": 8, "counterObservedAt": "2026-11-01T03:00:00Z",
                 "solverListObservedAt": "2026-11-01T03:01:00Z",
                 "crawl": {"status": "ok", "lastAttemptAt": "2026-11-01T03:00:00Z",
                           "lastSuccessAt": "2026-11-01T03:00:00Z"}}
        fetcher = FakeFetcher({url: "fresh html"})
        failed = {"pages": 0, "queued": 0, "skipped": 0,
                  "failedRegions": ["china"], "partialRegions": []}
        with patch.object(crawl_certitude, "parse_checker_html", return_value=fresh), \
                patch.object(crawl_certitude, "index_publications", return_value=failed):
            with self.assertRaisesRegex(RuntimeError, "dashboard_scan_failed"):
                self.collect(conn, fetcher)
        self.assertEqual(fetcher.calls, [url])
        self.assertFalse(conn.state["checkers"]["GC12345"]["activity"]["pending"])
        self.assertEqual(len(conn.state["snapshots"]), 1)

    def test_anonymous_checker_builds_json_and_publishes_nonempty_batch(self):
        parsed = parse_checker_html(checker_html(solvers="[]"),
                                    "https://www.certitudes.org/certitude?wp=GC12345",
                                    datetime(2026, 11, 1, tzinfo=timezone.utc))
        json.dumps(parsed, ensure_ascii=False)
        manifest, scopes, facts = build_dataset(
            [cache(archived=True)],
            {"GC12345": parsed},
        )
        self.assertEqual(scopes[0]["summary"]["cacheCount"], 1)
        self.assertEqual(facts["GC12345"]["checkers"][0]["publicSolvers"], [])
        output_dir = ROOT / (".test-certitude-output-" + uuid.uuid4().hex)
        try:
            destination = publish_dataset(manifest, scopes, facts, output_dir=output_dir)
            published = json.loads((output_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(published["datasetVersion"], manifest["datasetVersion"])
            self.assertFalse((destination / "caches").exists())
            self.assertEqual(len(list(output_dir.rglob("*.json"))), 16)
            self.assertTrue((destination / "scopes" / "all" / "archived.json").is_file())
        finally:
            if output_dir.exists() and output_dir.resolve().is_relative_to(ROOT.resolve()):
                shutil.rmtree(output_dir)


if __name__ == "__main__":
    unittest.main()
