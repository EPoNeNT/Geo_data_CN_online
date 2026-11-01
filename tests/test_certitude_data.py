"""Offline contract tests for the Certitude static dataset derivation module."""

import unittest
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from certitude_data import build_cache_detail, build_scope, validate_dataset


def checker(checker_id, role, solvers=(), coverage="complete", success=1, failure=1):
    solver_rows = [
        {
            "username": name,
            "normalizedUsername": name.strip().casefold(),
            "sourceUserId": None,
            "certifiedAt": None,
            "sourceRank": index + 1,
        }
        for index, name in enumerate(solvers)
    ]
    return {
        "checkerId": checker_id,
        "sourceUrl": f"https://certitudes.org/{checker_id}",
        "role": role,
        "stageLabel": role,
        "answerType": "keyword",
        "successCount": success,
        "failureCount": failure,
        "publicSolvers": solver_rows,
        "solverListCoverage": coverage,
        "solverRowsRetrieved": len(solvers),
        "publicSolverCount": len(solvers) if coverage == "complete" else None,
        "counterObservedAt": "2026-10-31T10:00:00Z",
        "publishedAt": None,
        "latestActivityAt": None,
        "solverListObservedAt": "2026-10-31T10:00:00Z",
        "crawl": {"status": "ok"},
    }


def cache(**overrides):
    value = {
        "code": "GC12345",
        "name": "Puzzle",
        "owner": "Owner",
        "ownerKey": "owner",
        "geocacheType": 8,
        "region": "china",
        "city": "Shanghai",
        "cacheStatus": 1,
        "detailsUrl": "https://coord.info/GC12345",
        "finalCheckerId": "Final",
        "finalCertitudeUrl": "https://certitudes.org/Final",
        "completionRule": "single_final",
        "completionCheckerIds": ["Final"],
        "checkerSetCoverage": "complete",
        "difficulty": None,
        "terrain": None,
    }
    value.update(overrides)
    return value


def manifest(version="v1", cache_count=1):
    return {
        "schemaVersion": 1,
        "datasetVersion": version,
        "generatedAt": "2026-11-01T00:00:00Z",
        "regions": ["all", "china", "taiwan", "hong-kong", "macao"],
        "availabilities": ["all", "active", "archived"],
        "scopePathTemplate": f"/data/certitude/versions/{version}/scopes/{{region}}/{{availability}}.json",
        "cachePathTemplate": f"/data/certitude/versions/{version}/caches/{{code}}.json",
        "cacheCount": cache_count,
        "coverage": {},
    }


class CertitudeDataContractTests(unittest.TestCase):
    def detail(self, c=None, checks=None, logs=None):
        return build_cache_detail(c or cache(), checks or [], logs or [])

    def test_username_lookup_trims_and_casefolds_but_preserves_punctuation(self):
        detail = self.detail(checks=[checker("Final", "final", ["  O'Neil-X  "])])
        record = detail["cache"]["playerRecords"]["o'neil-x"]
        self.assertEqual(record["username"], "O'Neil-X")
        self.assertEqual(record["solveStatus"], "certified")

    def test_checker_ids_are_case_sensitive_and_deduplicated_by_exact_id(self):
        checks = [checker("AbC", "stage"), checker("abc", "stage"), checker("AbC", "stage")]
        detail = self.detail(c=cache(finalCheckerId=None, finalCertitudeUrl=None,
                                     completionRule="unknown", completionCheckerIds=[]), checks=checks)
        ids = [row["checkerId"] for row in detail["cache"]["checkers"]]
        self.assertEqual(ids, ["AbC", "abc"])

    def test_solver_names_are_deduplicated_by_normalized_username(self):
        detail = self.detail(checks=[checker("Final", "final", [" Alice ", "alice"])])
        self.assertEqual(list(detail["cache"]["playerRecords"]), ["alice", "owner"])
        self.assertEqual(detail["stats"]["observedCount"], 1)

    def test_duplicate_source_user_id_deduplicates_renamed_solver_records(self):
        check = checker("Final", "final", ["OldName", "NewName"])
        check["publicSolvers"][0]["sourceUserId"] = "user-1"
        check["publicSolvers"][1]["sourceUserId"] = "user-1"
        detail = self.detail(checks=[check])
        self.assertEqual(detail["stats"]["observedCount"], 1)

    def test_checker_result_records_absence_with_coverage_and_metadata(self):
        final = checker("Final", "final", ["Solver"])
        stage = checker("Stage", "stage", [], coverage="complete")
        detail = self.detail(checks=[final, stage])
        results = {r["checkerId"]: r for r in detail["cache"]["playerRecords"]["solver"]["checkerResults"]}
        self.assertEqual(set(results), {"Final", "Stage"})
        self.assertEqual(results["Stage"]["status"], "no_public_record")
        self.assertEqual(results["Stage"]["solverListCoverage"], "complete")

    def test_missing_checker_result_is_unknown_when_list_not_complete(self):
        final = checker("Final", "final", ["Solver"])
        stage = checker("Stage", "stage", [], coverage="partial")
        detail = self.detail(checks=[final, stage])
        results = {r["checkerId"]: r for r in detail["cache"]["playerRecords"]["solver"]["checkerResults"]}
        self.assertEqual(results["Stage"]["status"], "unknown")

    def test_counter_coverage_tracks_valid_counter_pairs_independently(self):
        final = checker("Final", "final", success=3, failure=2)
        stage = checker("Stage", "stage", success=None, failure=2)
        detail = self.detail(checks=[final, stage])
        self.assertEqual(detail["stats"]["counterCoverage"], "partial")

    def test_empty_or_invalid_counter_pairs_have_unknown_coverage(self):
        empty = self.detail(checks=[])
        self.assertEqual(empty["stats"]["counterCoverage"], "unknown")
        negative = self.detail(checks=[checker("Final", "final", success=-1, failure=0)])
        self.assertEqual(negative["stats"]["counterCoverage"], "unknown")
        missing_observation = checker("Final", "final", success=1, failure=0)
        missing_observation["counterObservedAt"] = None
        detail = self.detail(checks=[missing_observation])
        self.assertEqual(detail["stats"]["counterCoverage"], "unknown")

    def test_complete_correct_count_depends_only_on_required_final_lists(self):
        final = checker("Final", "final", ["Solver"], coverage="complete")
        stage = checker("Stage", "stage", ["Solver"], coverage="unknown")
        detail = self.detail(checks=[final, stage])
        self.assertEqual(detail["stats"]["correctCount"], 1)
        self.assertTrue(detail["stats"]["complete"])

    def test_source_observation_times_and_lookup_coverage_use_final_lists(self):
        final = checker("Final", "final", ["Solver"], coverage="partial")
        final["solverListObservedAt"] = "2026-10-20T00:00:00Z"
        final["crawl"]["lastAttemptAt"] = "2026-10-22T00:00:00Z"
        stage = checker("Stage", "stage", ["Solver"], coverage="complete")
        stage["solverListObservedAt"] = "2026-10-10T00:00:00Z"
        stage["crawl"]["lastAttemptAt"] = "2026-10-21T00:00:00Z"
        c = cache(foundLogsAsOf="2026-10-30", foundLogsLastAttemptAt=None)
        detail = self.detail(c, [final, stage])
        record = detail["cache"]["playerRecords"]["solver"]
        self.assertEqual(record["sourceCoverage"]["certitudeObservedAt"], "2026-10-20T00:00:00Z")
        self.assertEqual(record["sourceCoverage"]["certitudeLastAttemptAt"], "2026-10-22T00:00:00Z")
        self.assertEqual(record["sourceCoverage"]["foundLogsAsOf"], "2026-10-30")
        self.assertEqual(detail["lookupCoverage"]["publicSolverLists"], "partial")

    def test_single_final_rejects_multiple_required_checker_ids(self):
        c = cache(completionCheckerIds=["Final", "Other"])
        detail = self.detail(c, [checker("Final", "final", ["Solver"]), checker("Other", "final")])
        self.assertFalse(detail["stats"]["knownRule"])

    def test_checker_rows_keep_raw_retrieval_count_and_set_deduped_public_count(self):
        final = checker("Final", "final", ["Alice", "alice"], coverage="complete")
        final["solverRowsRetrieved"] = 8
        detail = self.detail(checks=[final])
        row = detail["cache"]["checkers"][0]
        self.assertEqual(row["solverRowsRetrieved"], 8)
        self.assertEqual(row["publicSolverCount"], 1)
        self.assertEqual(row["publicSolvers"][0]["sourceUserId"], None)
        self.assertEqual(row["publicSolvers"][0]["sourceRank"], 1)

    def test_unknown_rule_keeps_stage_evidence_without_claiming_partial_or_complete_solve(self):
        detail = self.detail(c=cache(completionRule="unknown", completionCheckerIds=[], checkerSetCoverage="unknown"),
                             checks=[checker("Stage", "stage", ["Solver"])])
        row = detail["cache"]["playerRecords"]["solver"]
        self.assertEqual(row["solveStatus"], "unknown")
        self.assertEqual(row["checkerResults"][0]["status"], "certified")

    def test_known_unsatisfied_rule_with_stage_record_is_partial(self):
        detail = self.detail(checks=[checker("Stage", "stage", ["Solver"]), checker("Final", "final")])
        self.assertEqual(detail["cache"]["playerRecords"]["solver"]["solveStatus"], "partial")

    def test_single_final_uses_only_the_named_final_and_its_time(self):
        checks = [checker("Other", "stage", ["Solver"]), checker("Final", "final", ["Solver"])]
        checks[1]["publicSolvers"] = [{"username": "Solver", "certifiedAt": "2026-10-20T00:00:00Z"}]
        detail = self.detail(checks=checks)
        row = detail["cache"]["playerRecords"]["solver"]
        self.assertEqual(row["solveStatus"], "certified")
        self.assertEqual(row["certifiedAt"], "2026-10-20T00:00:00Z")

    def test_verified_final_is_enough_when_stage_checker_set_is_unknown(self):
        c = cache(checkerSetCoverage="unknown")
        detail = self.detail(c, [checker("Final", "final", ["Solver"]),
                                 checker("Stage", "stage", ["Solver"])])
        row = detail["cache"]["playerRecords"]["solver"]
        self.assertTrue(detail["stats"]["knownRule"])
        self.assertEqual(row["solveStatus"], "certified")
        self.assertIsNone(row["stageProgress"]["denominator"])

    def test_conflicting_final_roles_without_confirmed_rule_are_unknown(self):
        c = cache(completionRule="unknown", completionCheckerIds=[], checkerSetCoverage="unknown")
        detail = self.detail(c, [checker("F1", "final", ["Solver"]), checker("F2", "final")])
        self.assertEqual(detail["stats"]["knownRule"], False)
        self.assertEqual(detail["cache"]["playerRecords"]["solver"]["solveStatus"], "unknown")

    def test_any_final_requires_one_of_confirmed_finals(self):
        c = cache(completionRule="any_final", completionCheckerIds=["F1", "F2"])
        detail = self.detail(c, [checker("F1", "final", ["Solver"]), checker("F2", "final")])
        self.assertEqual(detail["cache"]["playerRecords"]["solver"]["solveStatus"], "certified")

    def test_all_final_requires_every_confirmed_final_and_latest_required_time(self):
        c = cache(completionRule="all_final", completionCheckerIds=["F1", "F2"])
        checks = [checker("F1", "final"), checker("F2", "final")]
        checks[0]["publicSolvers"] = [{"username": "Solver", "certifiedAt": "2026-10-20T00:00:00Z"}]
        checks[1]["publicSolvers"] = [{"username": "solver", "certifiedAt": "2026-10-22T00:00:00Z"}]
        detail = self.detail(c, checks)
        row = detail["cache"]["playerRecords"]["solver"]
        self.assertEqual(row["solveStatus"], "certified")
        self.assertEqual(row["certifiedAt"], "2026-10-22T00:00:00Z")

    def test_any_final_with_missing_certification_time_returns_null_time(self):
        c = cache(completionRule="any_final", completionCheckerIds=["F1", "F2"])
        detail = self.detail(c, [checker("F1", "final", ["Solver"]), checker("F2", "final")])
        self.assertEqual(detail["cache"]["playerRecords"]["solver"]["solveStatus"], "certified")
        self.assertIsNone(detail["cache"]["playerRecords"]["solver"]["certifiedAt"])

    def test_missing_required_final_prevents_all_final_completion(self):
        c = cache(completionRule="all_final", completionCheckerIds=["F1", "F2"])
        detail = self.detail(c, [checker("F1", "final", ["Solver"])])
        self.assertFalse(detail["stats"]["knownRule"])
        self.assertEqual(detail["cache"]["playerRecords"]["solver"]["solveStatus"], "unknown")

    def test_any_final_with_missing_required_checker_is_unknown(self):
        c = cache(completionRule="any_final", completionCheckerIds=["F1", "F2"])
        detail = self.detail(c, [checker("F1", "final", ["Solver"])])
        self.assertFalse(detail["stats"]["knownRule"])
        self.assertEqual(detail["cache"]["playerRecords"]["solver"]["solveStatus"], "unknown")

    def test_unverified_required_checker_mapping_cannot_establish_rule(self):
        final = checker("Final", "final", ["Solver"])
        final["mappingStatus"] = "unresolved"
        detail = self.detail(checks=[final])
        self.assertFalse(detail["stats"]["knownRule"])
        self.assertEqual(detail["cache"]["playerRecords"]["solver"]["solveStatus"], "unknown")

    def test_self_solve_is_excluded_from_solver_ranking_and_graph(self):
        detail = self.detail(checks=[checker("Final", "final", ["Owner", "Player"])])
        scope = build_scope([detail], "all", "all")
        self.assertNotIn("owner", [r["key"] for r in scope["rankings"]["solvers"]])
        self.assertFalse(any(e["solverKey"] == "owner" for e in scope["graph"]["edges"]))

    def test_partial_final_list_ranks_observed_distinct_players_but_correct_count_unknown(self):
        detail = self.detail(checks=[checker("Final", "final", ["A", "a", "B"], "partial")])
        stats = detail["stats"]
        self.assertEqual(stats["observedCount"], 2)
        self.assertIsNone(stats["correctCount"])
        self.assertEqual(stats["publicCorrectCount"], 2)

    def test_missing_counter_pair_makes_aggregate_counts_unknown(self):
        detail = self.detail(checks=[checker("Final", "final", success=None, failure=3)])
        self.assertIsNone(detail["stats"]["successCount"])
        self.assertIsNone(detail["stats"]["failureCount"])
        self.assertIsNone(detail["stats"]["attemptCount"])
        self.assertIsNone(detail["stats"]["submissionRate"])

    def test_zero_attempt_denominator_has_null_rate(self):
        detail = self.detail(checks=[checker("Final", "final", success=0, failure=0)])
        self.assertEqual(detail["stats"]["attemptCount"], 0)
        self.assertIsNone(detail["stats"]["submissionRate"])

    def test_puzzle_metric_ties_use_competition_ranks_and_stable_gc_order(self):
        details = [self.detail(cache(code=code, owner="Owner", ownerKey="owner"),
                               [checker("Final", "final", success=success, failure=0)])
                   for code, success in (("GC33333", 2), ("GC11111", 1), ("GC22222", 1))]
        scope = build_scope(details, "all", "all")
        rows = scope["puzzleRankings"]["attempts"]["asc"]
        self.assertEqual([r["code"] for r in rows], ["GC11111", "GC22222", "GC33333"])
        self.assertEqual([r["rank"] for r in rows], [1, 1, 3])

    def test_unknown_metrics_sort_last_in_both_directions(self):
        known = self.detail(cache(code="GC11111"), [checker("Final", "final", success=1, failure=1)])
        unknown = self.detail(cache(code="GC22222"), [checker("Final", "final", success=None, failure=None)])
        scope = build_scope([unknown, known], "all", "all")
        for direction in ("asc", "desc"):
            self.assertEqual(scope["puzzleRankings"]["attempts"][direction][-1]["code"], "GC22222")

    def test_scope_availability_excludes_null_and_archived_from_active(self):
        rows = [self.detail(cache(code="GC11111", cacheStatus=1)),
                self.detail(cache(code="GC22222", cacheStatus=None)),
                self.detail(cache(code="GC33333", cacheStatus=2))]
        scope = build_scope(rows, "all", "active")
        self.assertEqual([r["code"] for r in scope["caches"]], ["GC11111"])

    def test_graph_owner_order_uses_edge_weight_not_puzzle_count(self):
        details = [self.detail(cache(code=f"GC{i:05d}", owner="ManyPuzzles", ownerKey="many"),
                               [checker("Final", "final", [f"S{i}"])]) for i in range(1, 4)]
        details.append(self.detail(cache(code="GC00004", owner="OnePuzzle", ownerKey="one"),
                                   [checker("Final", "final", ["S1", "S2", "S3", "S4"])]))
        scope = build_scope(details, "all", "all")
        self.assertEqual(scope["rankings"]["owners"][0]["key"], "many")
        self.assertEqual(scope["graph"]["order"]["owners"][0], "one")

    def test_unknown_owner_keeps_solver_without_inventing_edge(self):
        detail = self.detail(cache(owner=None, ownerKey=None), [checker("Final", "final", ["Solver"])])
        scope = build_scope([detail], "all", "all")
        self.assertIn("solver", [r["key"] for r in scope["rankings"]["solvers"]])
        self.assertEqual(scope["graph"]["edges"], [])

    def test_solver_owner_count_is_distinct_known_owners_across_solved_caches(self):
        details = [
            self.detail(cache(code="GC10001", owner="Owner A", ownerKey="owner-a"),
                        [checker("Final", "final", ["Solver"])]),
            self.detail(cache(code="GC10002", owner="Owner A", ownerKey="owner-a"),
                        [checker("Final", "final", ["Solver"])]),
            self.detail(cache(code="GC10003", owner="Owner B", ownerKey="owner-b"),
                        [checker("Final", "final", ["Solver"])])]
        scope = build_scope(details, "all", "all")
        row = next(r for r in scope["rankings"]["solvers"] if r["key"] == "solver")
        self.assertEqual(row["solvedCount"], 3)
        self.assertEqual(row["ownerCount"], 2)

    def test_validator_rejects_self_consistent_scope_forged_against_details(self):
        m, scopes, details = self.valid_dataset()
        all_scope = next(s for s in scopes if s["scope"] == {"region": "all", "availability": "all"})
        # Keep its local counters and rankings consistent while removing the actual cache fact.
        all_scope["caches"] = []
        all_scope["summary"] = {"cacheCount": 0, "solverCount": 0, "ownerCount": 0, "certificationCount": 0}
        all_scope["rankings"] = {"solvers": [], "owners": []}
        all_scope["puzzleRankings"] = {metric: {direction: [] for direction in ("asc", "desc")}
                                         for metric in ("rate", "correct", "attempts")}
        all_scope["graph"] = {"players": [], "edges": [], "order": {"owners": [], "solvers": []}}
        all_scope["coverage"] = {"ruleUnknownCacheCount": 0, "incompleteSolverCacheCount": 0, "noOwnerCacheCount": 0}
        with self.assertRaises(ValueError):
            validate_dataset(m, scopes, details)

    def test_validator_rejects_wrong_region_membership_and_invalid_source_count(self):
        m, scopes, details = self.valid_dataset()
        details["GC12345"]["cache"]["region"] = "taiwan"
        with self.assertRaises(ValueError):
            validate_dataset(m, scopes, details)
        m, scopes, details = self.valid_dataset()
        details["GC12345"]["cache"]["checkers"][0]["successCount"] = -1
        with self.assertRaises(ValueError):
            validate_dataset(m, scopes, details)

    def test_found_date_without_time_does_not_invent_timestamp(self):
        detail = self.detail(checks=[checker("Final", "final", ["Solver"])],
                             logs=[{"username": "Solver", "foundDate": "2026-10-20", "foundAt": None}])
        row = detail["cache"]["playerRecords"]["solver"]
        self.assertEqual(row["foundDate"], "2026-10-20")
        self.assertIsNone(row["foundAt"])
        self.assertEqual(row["foundStatus"], "found")

    def test_absent_found_log_without_complete_coverage_is_unknown(self):
        detail = self.detail(checks=[checker("Final", "final", ["Solver"])])
        self.assertEqual(detail["cache"]["playerRecords"]["solver"]["foundStatus"], "unknown")

    def test_sparse_player_records_include_stage_final_owner_and_found_users(self):
        detail = self.detail(checks=[checker("Stage", "stage", ["StageUser"]),
                                     checker("Final", "final", ["FinalUser"])],
                             logs=[{"username": "FoundUser", "foundDate": None, "foundAt": None}])
        records = detail["cache"]["playerRecords"]
        self.assertTrue({"stageuser", "finaluser", "owner", "founduser"}.issubset(records))

    def test_large_solver_list_preserves_sparse_player_and_option_counts(self):
        names = [f"Solver{i:04d}" for i in range(4387)]
        detail = self.detail(checks=[checker("Final", "final", names)])
        self.assertEqual(detail["stats"]["observedCount"], 4387)
        self.assertEqual(len(detail["certifiedSolvers"]), 4387)
        self.assertEqual(len(detail["playerOptions"]), 4388)
        self.assertEqual(len(detail["cache"]["playerRecords"]), 4388)

    def test_validation_rejects_mixed_version_missing_detail_and_wrong_cache_count(self):
        m = manifest(cache_count=2)
        scope = build_scope([], "all", "all")
        with self.assertRaises(ValueError):
            validate_dataset(m, [scope], {})
        m = manifest(cache_count=0)
        scope["schemaVersion"] = 1
        scope["datasetVersion"] = "other"
        scope["generatedAt"] = m["generatedAt"]
        with self.assertRaises(ValueError):
            validate_dataset(m, [scope], {})

    def valid_dataset(self):
        m = manifest(cache_count=1)
        detail = self.detail(checks=[checker("Final", "final", ["Solver"])])
        detail.update({"schemaVersion": 1, "datasetVersion": "v1", "generatedAt": m["generatedAt"]})
        scopes = []
        for region in m["regions"]:
            for availability in m["availabilities"]:
                scope = build_scope([detail], region, availability)
                scope.update({"schemaVersion": 1, "datasetVersion": "v1", "generatedAt": m["generatedAt"]})
                scopes.append(scope)
        return m, scopes, {"GC12345": detail}

    def test_validator_accepts_complete_consistent_dataset(self):
        m, scopes, details = self.valid_dataset()
        self.assertTrue(validate_dataset(m, scopes, details))

    def test_validator_rejects_bad_manifest_version_time_and_paths(self):
        for field, value in (("schemaVersion", 2), ("generatedAt", "not-a-date"),
                             ("scopePathTemplate", "/wrong/{region}/{availability}.json")):
            m, scopes, details = self.valid_dataset()
            m[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate_dataset(m, scopes, details)

    def test_validator_rejects_missing_scope_and_detail_reference(self):
        m, scopes, details = self.valid_dataset()
        with self.assertRaises(ValueError):
            validate_dataset(m, scopes[:-1], details)
        m, scopes, details = self.valid_dataset()
        scopes[0]["caches"] = [{"code": "GC99999", "stats": {}}]
        scopes[0]["summary"]["cacheCount"] = 1
        with self.assertRaises(ValueError):
            validate_dataset(m, scopes, details)

    def test_validator_rejects_rank_graph_stat_and_count_mismatches(self):
        mutations = (
            lambda s: s[0]["puzzleRankings"]["rate"]["asc"].append({"code": "GC99999", "rank": 2}),
            lambda s: s[0]["graph"]["edges"].append({"ownerKey": "x", "solverKey": "x", "count": 2, "cacheCodes": ["GC12345"]}),
            lambda s: s[0]["caches"][0].update({"stats": {**s[0]["caches"][0]["stats"], "observedCount": 99}}),
            lambda s: s[0]["summary"].update({"cacheCount": 99}),
        )
        for index, mutate in enumerate(mutations):
            m, scopes, details = self.valid_dataset()
            mutate(scopes)
            with self.subTest(index=index), self.assertRaises(ValueError):
                validate_dataset(m, scopes, details)


if __name__ == "__main__":
    unittest.main()
