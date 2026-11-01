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
        self.state = {"descriptions": {}, "checkers": {}, "snapshots": []}


class FakeStore:
    def __init__(self, connection):
        self.state = connection.state

    def ensure_schema(self):
        pass

    def load_descriptions(self):
        return copy.deepcopy(self.state["descriptions"])

    def load_checkers(self):
        return copy.deepcopy(self.state["checkers"])

    def save_description(self, code, record):
        self.state["descriptions"][code] = copy.deepcopy(record)

    def save_checker(self, record, snapshot=None):
        self.state["checkers"][record["checkerId"]] = copy.deepcopy(record)
        if snapshot is not None:
            self.state["snapshots"].append(copy.deepcopy(snapshot))


class FakeSession:
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


def description_html(link=None):
    anchor = f'<a href="{link}">Certitude</a>' if link else ""
    return f'<div id="ShortDescription">{anchor}</div><div id="LongDescription"></div>'


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
            patch.object(crawl_certitude, "load_candidates", return_value=[]),
            patch.object(crawl_certitude, "optional_cookie", return_value="offline-cookie"),
            patch.object(crawl_certitude, "create_geocaching_session", return_value=FakeSession()),
            patch.object(crawl_certitude.requests, "Session", return_value=FakeSession()),
        ]
        for active in self.patches:
            active.start()
            self.addCleanup(active.stop)

    def collect(self, conn, fetcher, *, limit=200, logger=None):
        return crawl_certitude.collect(conn, description_limit=limit, refresh_days=30,
                                       fetcher=fetcher, logger=logger)

    def test_archived_candidates_are_scanned_and_limited_work_resumes(self):
        conn = FakeConnection()
        rows = [cache("GC00001"), cache("GC00002"), cache("GC00003")]
        crawl_certitude.load_candidates = lambda _conn: rows
        fetcher = FakeFetcher({"default": description_html()})
        self.collect(conn, fetcher, limit=1)
        self.assertEqual(len(fetcher.calls), 1)
        self.assertIn("GC00001", conn.state["descriptions"])
        self.collect(conn, fetcher, limit=1)
        self.assertEqual(len(fetcher.calls), 2)
        self.assertIn("GC00002", conn.state["descriptions"])
        self.assertEqual(conn.state["descriptions"]["GC00001"]["descriptionScanStatus"], "ok")

    def test_retained_verified_checker_is_refreshed_without_new_description_scan(self):
        conn = FakeConnection()
        row = cache(archived=False)
        crawl_certitude.load_candidates = lambda _conn: [row]
        old = parse_checker_html(checker_html(), "https://www.certitudes.org/certitude?wp=GC12345",
                                 datetime(2026, 10, 31, tzinfo=timezone.utc))
        old["sourceUrl"] = "https://www.certitudes.org/certitude?wp=GC12345"
        old["successCount"], old["failureCount"] = 1, 1
        old["counterObservedAt"] = "2026-10-31T00:00:00Z"
        conn.state["checkers"][old["checkerId"]] = old
        conn.state["descriptions"]["GC12345"] = {
            "descriptionScannedAt": crawl_certitude.utc_now(), "descriptionScanStatus": "ok",
            "descriptionScanCoverage": "complete",
            "sourceFingerprint": crawl_certitude.description_fingerprint(row), "descriptionLinks": [],
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
        self.assertEqual(fetcher.calls, [old["sourceUrl"]])
        self.assertEqual(conn.state["checkers"][old["checkerId"]]["successCount"], 0)
        self.assertEqual(len(conn.state["snapshots"]), 1)

    def test_checker_fetch_error_retains_pair_list_and_emits_no_snapshot(self):
        conn = FakeConnection()
        old = parse_checker_html(checker_html(), "https://www.certitudes.org/certitude?wp=GC12345",
                                 datetime(2026, 10, 31, tzinfo=timezone.utc))
        old.update(sourceUrl="https://www.certitudes.org/certitude?wp=GC12345",
                   successCount=7, failureCount=3, counterObservedAt="2026-10-31T00:00:00Z",
                   publicSolvers=[{"username": "Ada"}], solverListCoverage="complete",
                   solverListObservedAt="2026-10-31T00:00:00Z")
        conn.state["checkers"]["GC12345"] = old
        fetcher = FakeFetcher({old["sourceUrl"]: crawl_certitude.FetchError("http_500")})
        self.collect(conn, fetcher)
        saved = conn.state["checkers"]["GC12345"]
        self.assertEqual((saved["successCount"], saved["failureCount"]), (7, 3))
        self.assertEqual(saved["counterObservedAt"], old["counterObservedAt"])
        self.assertEqual(saved["publicSolvers"], old["publicSolvers"])
        self.assertEqual(saved["solverListObservedAt"], old["solverListObservedAt"])
        self.assertEqual(saved["crawl"]["status"], "error")
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
                 "crawl": {"status": "ok", "lastAttemptAt": "2026-11-01T00:00:00Z",
                           "lastSuccessAt": "2026-11-01T00:00:00Z"}}
        with patch.object(crawl_certitude, "parse_checker_html", return_value=fresh):
            self.collect(conn, FakeFetcher({old["sourceUrl"]: "html"}))
        self.assertEqual(len(conn.state["snapshots"]), 1)
        self.assertEqual(conn.state["snapshots"][0]["successCount"], 0)
        self.assertEqual(conn.state["snapshots"][0]["observedAt"], fresh["counterObservedAt"])

    def test_failed_pending_waypoint_keeps_link_and_attempt_error(self):
        conn = FakeConnection()
        crawl_certitude.load_candidates = lambda _conn: [cache()]
        link = {"sourceUrl": "https://www.certitudes.org/certitude?wp=NewID",
                "sourceWaypoint": "NewID", "checkerId": None, "mappingStatus": "pending"}
        conn.state["descriptions"]["GC12345"] = {
            "descriptionScannedAt": crawl_certitude.utc_now(), "descriptionScanStatus": "ok",
            "descriptionScanCoverage": "complete", "sourceFingerprint":
                crawl_certitude.description_fingerprint(cache()), "descriptionLinks": [link],
        }
        fetcher = FakeFetcher({link["sourceUrl"]: crawl_certitude.FetchError("http_500")})
        with self.assertRaisesRegex(RuntimeError, "no_valid_checker_observation"):
            self.collect(conn, fetcher)
        saved = conn.state["descriptions"]["GC12345"]["descriptionLinks"][0]
        self.assertEqual(saved["sourceWaypoint"], "NewID")
        self.assertEqual(saved["checkerId"], "NewID")
        self.assertEqual(saved["mappingStatus"], "pending")
        self.assertEqual(saved.get("errorCode"), "http_500",
                         (fetcher.calls, conn.state["descriptions"]))
        self.assertIn("lastAttemptAt", saved)
        self.assertIn("NewID", conn.state["checkers"])
        pending = conn.state["checkers"]["NewID"]
        self.assertIsNone(pending["cacheCode"])
        self.assertEqual(pending["mappingStatus"], "pending")
        self.assertEqual(pending["crawl"]["status"], "error")
        self.assertEqual(pending["crawl"]["errorCode"], "http_500")
        self.assertEqual(pending["sourceUrl"], link["sourceUrl"])

    def test_auth_failure_stops_description_batch_without_marking_remaining_complete(self):
        conn = FakeConnection()
        rows = [cache("GC00001", premium=True), cache("GC00002", premium=False)]
        crawl_certitude.load_candidates = lambda _conn: rows
        cookie_patch = patch.object(crawl_certitude, "optional_cookie", return_value=None)
        with cookie_patch:
            with self.assertRaisesRegex(RuntimeError, "no_valid_certitude_discovery"):
                self.collect(conn, FakeFetcher({}))
        self.assertEqual(conn.state["descriptions"]["GC00001"]["descriptionScanStatus"], "error")
        self.assertNotIn("GC00002", conn.state["descriptions"])

    def test_description_fetch_failure_does_not_create_fake_empty_success(self):
        conn = FakeConnection()
        crawl_certitude.load_candidates = lambda _conn: [cache()]
        old_link = {"sourceUrl": "https://www.certitudes.org/certitude?wp=OldID",
                    "checkerId": "OldID", "mappingStatus": "verified", "evidence": "old source"}
        conn.state["descriptions"]["GC12345"] = {"descriptionLinks": [old_link],
                                                  "descriptionScanStatus": "ok"}
        fetcher = FakeFetcher({"default": crawl_certitude.FetchError("http_500")})
        with self.assertRaisesRegex(RuntimeError, "no_valid_"):
            self.collect(conn, fetcher)
        saved = conn.state["descriptions"]["GC12345"]
        self.assertEqual(saved["descriptionScanStatus"], "error")
        self.assertEqual(saved["descriptionLinks"][0]["sourceUrl"], old_link["sourceUrl"])
        self.assertEqual(saved["descriptionLinks"][0]["mappingStatus"], "verified")
        self.assertEqual(saved["descriptionLinks"][0]["errorCode"], "http_500")
        self.assertIn("lastAttemptAt", saved["descriptionLinks"][0])
        self.assertNotEqual(saved["descriptionScanCoverage"], "complete")

    def test_anonymous_checker_builds_json_and_publishes_nonempty_batch(self):
        parsed = parse_checker_html(checker_html(solvers="[]"),
                                    "https://www.certitudes.org/certitude?wp=GC12345",
                                    datetime(2026, 11, 1, tzinfo=timezone.utc))
        json.dumps(parsed, ensure_ascii=False)
        manifest, scopes, details = build_dataset(
            [cache(archived=True)],
            {"GC12345": {"descriptionScanStatus": "ok", "descriptionScanCoverage": "complete",
                         "descriptionLinks": []}},
            {"GC12345": parsed},
        )
        self.assertEqual(manifest["cacheCount"], 1)
        self.assertEqual(details["GC12345"]["cache"]["checkers"][0]["publicSolvers"], [])
        output_dir = ROOT / (".test-certitude-output-" + uuid.uuid4().hex)
        try:
            destination = publish_dataset(manifest, scopes, details, output_dir=output_dir)
            published = json.loads((output_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(published["cacheCount"], 1)
            self.assertTrue((destination / "caches" / "GC12345.json").is_file())
            self.assertGreater((destination / "caches" / "GC12345.json").stat().st_size, 0)
        finally:
            if output_dir.exists() and output_dir.resolve().is_relative_to(ROOT.resolve()):
                shutil.rmtree(output_dir)


if __name__ == "__main__":
    unittest.main()
