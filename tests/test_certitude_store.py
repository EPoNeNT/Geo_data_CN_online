import copy
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from certitude_store import CertitudeStore, merge_checker


class FakeCursor:
    def __init__(self):
        self.calls = []
        self.rows = []

    def execute(self, query, params=None):
        self.calls.append((query, params))

    def fetchall(self):
        return self.rows

    def close(self):
        pass


class FakeConnection:
    def __init__(self):
        self.cur = FakeCursor()
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return self.cur

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


class CertitudeStoreTests(unittest.TestCase):
    def test_request_failure_retains_observations_and_updates_attempt(self):
        previous = {
            "checkerId": "CaseId", "successCount": 9, "failureCount": 2,
            "counterObservedAt": "2026-10-30T00:00:00Z",
            "publicSolvers": [{"username": "Ada"}], "solverListCoverage": "complete",
            "solverListObservedAt": "2026-10-29T00:00:00Z",
            "crawl": {"status": "ok", "lastSuccessAt": "2026-10-30T00:00:00Z"},
        }
        merged = merge_checker(previous, None, last_attempt_at="2026-11-01T00:00:00Z", error_code="timeout")
        self.assertEqual((merged["successCount"], merged["failureCount"]), (9, 2))
        self.assertEqual(merged["counterObservedAt"], previous["counterObservedAt"])
        self.assertEqual(merged["publicSolvers"], previous["publicSolvers"])
        self.assertEqual(merged["solverListObservedAt"], previous["solverListObservedAt"])
        self.assertEqual(merged["crawl"]["lastSuccessAt"], previous["crawl"]["lastSuccessAt"])
        self.assertEqual(merged["crawl"]["status"], "error")
        self.assertEqual(merged["crawl"]["errorCode"], "timeout")

    def test_null_counter_pair_never_mixes_with_previous_pair(self):
        previous = {"successCount": 3, "failureCount": 4, "counterObservedAt": "2026-10-30T00:00:00Z"}
        incoming = {"successCount": 0, "failureCount": None, "counterObservedAt": "2026-10-31T00:00:00Z",
                    "crawl": {"status": "ok"}}
        merged = merge_checker(previous, incoming, last_attempt_at="attempt")
        self.assertEqual((merged["successCount"], merged["failureCount"], merged["counterObservedAt"]),
                         (3, 4, "2026-10-30T00:00:00Z"))

    def test_valid_zero_pair_is_a_new_observation(self):
        merged = merge_checker({}, {"successCount": 0, "failureCount": 0,
                                    "counterObservedAt": "2026-11-01T00:00:00Z", "crawl": {"status": "ok"}})
        self.assertEqual((merged["successCount"], merged["failureCount"], merged["counterObservedAt"]),
                         (0, 0, "2026-11-01T00:00:00Z"))

    def test_incomplete_solver_list_does_not_replace_prior_complete_list(self):
        previous = {"publicSolvers": [{"username": "Ada"}], "solverRowsRetrieved": 1,
                    "solverListCoverage": "complete", "solverListObservedAt": "2026-10-30T00:00:00Z"}
        incoming = {"publicSolvers": [{"username": "Grace"}], "solverRowsRetrieved": 1,
                    "solverListCoverage": "partial", "solverListObservedAt": "2026-10-31T00:00:00Z",
                    "crawl": {"status": "ok"}}
        merged = merge_checker(previous, incoming)
        self.assertEqual(merged["publicSolvers"], [{"username": "Ada"}])
        self.assertEqual(merged["solverListCoverage"], "complete")
        self.assertEqual(merged["solverListObservedAt"], "2026-10-30T00:00:00Z")

    def test_mapping_conflict_is_explicit_and_keeps_verified_parent(self):
        previous = {"cacheCode": "GCAbC", "mappingStatus": "verified", "role": "final",
                    "mappingEvidence": "confirmed"}
        incoming = {"checkerId": "CaseId", "cacheCode": "GCXYZ", "mappingStatus": "verified",
                    "role": "stage", "mappingEvidence": "other", "crawl": {"status": "ok"}}
        merged = merge_checker(previous, incoming)
        self.assertEqual(merged["cacheCode"], "GCAbC")
        self.assertEqual(merged["mappingStatus"], "conflict")
        self.assertEqual(merged["mappingEvidence"], "confirmed; other")
        self.assertEqual(merged["role"], "unknown")

    def test_checker_snapshot_is_only_written_when_explicitly_passed(self):
        conn = FakeConnection()
        store = CertitudeStore(conn)
        store.save_checker({"checkerId": "CaseID", "crawl": {"status": "ok"}})
        statements = [q.lower() for q, _ in conn.cur.calls]
        self.assertTrue(any("insert into certitude_checkers" in q for q in statements))
        self.assertFalse(any("insert into certitude_snapshots" in q for q in statements))
        self.assertEqual(conn.commits, 1)

    def test_explicit_fresh_snapshot_is_inserted_atomically(self):
        conn = FakeConnection()
        record = {"checkerId": "CaseID", "crawl": {"status": "ok"}}
        snapshot = {"observedAt": "2026-11-01T00:00:00Z", "successCount": 0,
                    "failureCount": 0, "publicSolverCount": 0, "solverListCoverage": "complete"}
        CertitudeStore(conn).save_checker(record, snapshot=snapshot)
        self.assertEqual(len(conn.cur.calls), 2)
        self.assertIn("certitude_snapshots", conn.cur.calls[1][0])
        self.assertEqual(conn.commits, 1)

    def test_snapshot_is_rejected_for_uncrawl_status(self):
        conn = FakeConnection()
        snapshot = {"observedAt": "now", "successCount": 0, "failureCount": 0}
        with self.assertRaises(ValueError):
            CertitudeStore(conn).save_checker(
                {"checkerId": "CaseID", "crawl": {"status": "not_collected"}}, snapshot=snapshot
            )
        self.assertEqual(conn.cur.calls, [])

    def test_checker_id_is_parameterized_without_case_folding(self):
        conn = FakeConnection()
        CertitudeStore(conn).save_checker({"checkerId": "AbCd", "crawl": {"status": "ok"}})
        query, params = conn.cur.calls[0]
        self.assertNotIn("AbCd", query)
        self.assertEqual(params[0], "AbCd")
        self.assertEqual(json.loads(params[1])["checkerId"], "AbCd")

    def test_schema_is_additive(self):
        sql = (ROOT / "sql" / "certitude_setup.sql").read_text(encoding="utf-8").lower()
        self.assertIn("create table if not exists certitude_descriptions", sql)
        self.assertIn("create table if not exists certitude_checkers", sql)
        self.assertIn("create table if not exists certitude_snapshots", sql)
        self.assertNotIn("drop table", sql)

    def test_description_uses_contract_scanned_timestamp_and_preserves_link_evidence(self):
        conn = FakeConnection()
        prior = {"descriptionLinks": [
            {"checkerId": "X", "sourceUrl": "u", "mappingStatus": "verified", "evidence": "anchor A"},
            {"checkerId": "X", "sourceUrl": "u", "mappingStatus": "verified", "evidence": "anchor B"},
        ]}
        conn.cur.rows = [(json.dumps(prior),)]
        CertitudeStore(conn).save_description("GC123", {
            "descriptionLinks": [], "descriptionScannedAt": "2026-11-01T01:00:00Z",
            "lastAttemptAt": "2026-11-01T01:01:00Z",
        })
        query, params = conn.cur.calls[-1]
        saved = json.loads(params[1])
        self.assertEqual(params[3], "2026-11-01T01:00:00Z")
        self.assertEqual([r["evidence"] for r in saved["descriptionLinks"]], ["anchor A", "anchor B"])

    def test_partial_solver_list_requires_a_new_observation_and_keeps_prior_valid_partial(self):
        previous = {"publicSolvers": [{"username": "Ada"}], "solverRowsRetrieved": 1,
                    "solverListCoverage": "partial", "solverListObservedAt": "2026-10-31T00:00:00Z"}
        no_observation = {"publicSolvers": [], "solverListCoverage": "unknown",
                          "crawl": {"status": "ok"}}
        merged = merge_checker(previous, no_observation)
        self.assertEqual(merged["publicSolvers"], [{"username": "Ada"}])
        self.assertEqual(merged["solverListCoverage"], "partial")
        self.assertEqual(merged["solverListObservedAt"], previous["solverListObservedAt"])

    def test_new_partial_solver_observation_can_replace_previous_partial(self):
        previous = {"publicSolvers": [{"username": "Ada"}], "solverRowsRetrieved": 1,
                    "solverListCoverage": "partial", "solverListObservedAt": "2026-10-31T00:00:00Z"}
        incoming = {"publicSolvers": [{"username": "Grace"}], "solverRowsRetrieved": 1,
                    "solverListCoverage": "partial", "solverListObservedAt": "2026-11-01T00:00:00Z",
                    "crawl": {"status": "ok"}}
        merged = merge_checker(previous, incoming)
        self.assertEqual(merged["publicSolvers"], [{"username": "Grace"}])

    def test_known_metadata_survives_null_or_unknown_incoming_values(self):
        previous = {"publishedAt": "2020-01-01T00:00:00Z", "latestActivityAt": "2025-01-01T00:00:00Z",
                    "sourceTimezone": "Europe/Paris", "answerType": "coordinate",
                    "matchingMode": "exact", "role": "final"}
        incoming = {"publishedAt": None, "latestActivityAt": None, "sourceTimezone": None,
                    "answerType": "unknown", "matchingMode": "unknown", "role": "unknown",
                    "crawl": {"status": "ok"}}
        merged = merge_checker(previous, incoming)
        for key, value in previous.items():
            self.assertEqual(merged[key], value)

    def test_checker_id_change_is_rejected(self):
        with self.assertRaises(ValueError):
            merge_checker({"checkerId": "CaseID"}, {"checkerId": "caseid"})

    def test_conflict_preserves_both_parent_evidence_summaries(self):
        previous = {"cacheCode": "GCABC", "mappingStatus": "verified", "mappingEvidence": "description parent"}
        incoming = {"cacheCode": "GCXYZ", "mappingStatus": "verified", "mappingEvidence": "page parent",
                    "crawl": {"status": "ok"}}
        merged = merge_checker(previous, incoming)
        self.assertEqual(merged["mappingConflictEvidence"], {
            "previousCacheCode": "GCABC", "previousEvidence": "description parent",
            "conflictingCacheCode": "GCXYZ", "conflictingEvidence": "page parent",
        })

    def test_existing_mapping_conflict_survives_later_valid_parent_fetch(self):
        conflicted = {
            "checkerId": "OpaqueID", "cacheCode": "GCABC", "mappingStatus": "conflict",
            "mappingEvidence": "original evidence; first conflict evidence", "role": "unknown",
            "mappingConflictEvidence": {
                "previousCacheCode": "GCABC", "previousEvidence": "original evidence",
                "conflictingCacheCode": "GCXYZ", "conflictingEvidence": "first conflict evidence",
            },
        }
        next_fetch = {
            "checkerId": "OpaqueID", "cacheCode": "GCNEW", "mappingStatus": "verified",
            "mappingEvidence": "later conflicting parent evidence", "role": "final",
            "crawl": {"status": "ok"},
        }
        merged = merge_checker(conflicted, next_fetch)
        self.assertEqual(merged["cacheCode"], "GCABC")
        self.assertEqual(merged["mappingStatus"], "conflict")
        self.assertEqual(merged["role"], "unknown")
        self.assertEqual(merged["mappingConflictEvidence"]["previousCacheCode"], "GCABC")
        self.assertIn({"cacheCode": "GCNEW", "evidence": "later conflicting parent evidence"},
                      merged["mappingConflictEvidence"]["additionalConflicts"])
        repeated = merge_checker(merged, next_fetch)
        self.assertEqual(len(repeated["mappingConflictEvidence"]["additionalConflicts"]), 1)

    def test_retained_counter_pair_recomputes_success_rate(self):
        previous = {"successCount": 3, "failureCount": 7,
                    "counterObservedAt": "2026-10-31T00:00:00Z", "successRate": 0.3}
        incoming = {"successCount": None, "failureCount": 5,
                    "counterObservedAt": None, "successRate": None,
                    "crawl": {"status": "ok"}}
        merged = merge_checker(previous, incoming)
        self.assertEqual((merged["successCount"], merged["failureCount"]), (3, 7))
        self.assertEqual(merged["counterObservedAt"], previous["counterObservedAt"])
        self.assertEqual(merged["successRate"], 0.3)

    def test_success_rate_is_null_for_zero_denominator_or_missing_pair(self):
        zero = merge_checker(
            {"successCount": 0, "failureCount": 0, "counterObservedAt": "2026-10-31T00:00:00Z",
             "successRate": 0.5},
            {"successCount": None, "failureCount": None, "counterObservedAt": None,
             "crawl": {"status": "ok"}},
        )
        self.assertIsNone(zero["successRate"])
        missing = merge_checker({"successRate": 0.5}, {"successRate": None, "crawl": {"status": "ok"}})
        self.assertIsNone(missing["successRate"])

    def test_first_valid_counter_pair_retains_explicit_unknown_solver_list_fields(self):
        incoming = {
            "checkerId": "FirstID", "successCount": 2, "failureCount": 1,
            "counterObservedAt": "2026-11-01T00:00:00Z", "successRate": 2 / 3,
            "publicSolvers": [], "solverRowsRetrieved": 0, "solverReportedTotal": None,
            "publicSolverCount": None, "solverListCoverage": "unknown", "solverListObservedAt": None,
            "crawl": {"status": "ok", "fieldCoverage": "partial"},
        }
        merged = merge_checker(None, incoming)
        self.assertEqual(merged["publicSolvers"], [])
        self.assertEqual(merged["solverRowsRetrieved"], 0)
        self.assertIsNone(merged["solverReportedTotal"])
        self.assertIsNone(merged["publicSolverCount"])
        self.assertEqual(merged["solverListCoverage"], "unknown")
        self.assertIsNone(merged["solverListObservedAt"])

    def test_first_valid_solver_list_retains_explicit_unknown_counter_fields(self):
        incoming = {
            "checkerId": "FirstID", "successCount": None, "failureCount": None,
            "counterObservedAt": None, "successRate": None,
            "publicSolvers": [{"username": "Ada"}], "solverRowsRetrieved": 1,
            "solverReportedTotal": None, "publicSolverCount": 1,
            "solverListCoverage": "complete", "solverListObservedAt": "2026-11-01T00:00:00Z",
            "crawl": {"status": "ok", "fieldCoverage": "partial"},
        }
        merged = merge_checker(None, incoming)
        self.assertIsNone(merged["successCount"])
        self.assertIsNone(merged["failureCount"])
        self.assertIsNone(merged["counterObservedAt"])
        self.assertIsNone(merged["successRate"])
        self.assertEqual(merged["publicSolvers"], [{"username": "Ada"}])

    def test_counter_pair_requires_nonnegative_counts_and_aware_timestamp(self):
        for pair in (
            {"successCount": -1, "failureCount": 0, "counterObservedAt": "2026-11-01T00:00:00Z"},
            {"successCount": 0, "failureCount": 0, "counterObservedAt": "not-a-time"},
            {"successCount": 0, "failureCount": 0, "counterObservedAt": "2026-11-01T00:00:00"},
        ):
            with self.subTest(pair=pair):
                merged = merge_checker({"successCount": 4, "failureCount": 2,
                                        "counterObservedAt": "2026-10-31T00:00:00Z"},
                                       {**pair, "crawl": {"status": "ok"}})
                self.assertEqual((merged["successCount"], merged["failureCount"]),
                                 (4, 2))

    def test_snapshot_checker_id_must_match_record(self):
        conn = FakeConnection()
        with self.assertRaises(ValueError):
            CertitudeStore(conn).save_checker(
                {"checkerId": "CorrectID", "crawl": {"status": "ok"}},
                {"checkerId": "wrong", "observedAt": "2026-11-01T00:00:00Z",
                 "successCount": 1, "failureCount": 0},
            )
        self.assertEqual(conn.cur.calls, [])


if __name__ == "__main__":
    unittest.main()
