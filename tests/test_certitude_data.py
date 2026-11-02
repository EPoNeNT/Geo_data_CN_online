"""Offline tests for Certitude summary facts and scope derivation."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from certitude_data import (AVAILABILITIES, REGIONS, build_cache_facts, build_scope,
                            validate_dataset)


def checker(cid="Final", role="final", solvers=(), coverage="complete", success=1, failure=1):
    return {
        "checkerId": cid, "sourceUrl": f"https://certitudes.org/{cid}", "role": role,
        "successCount": success, "failureCount": failure,
        "counterObservedAt": "2026-10-31T10:00:00Z", "solverListCoverage": coverage,
        "solverRowsRetrieved": len(solvers),
        "publicSolverCount": len(solvers) if coverage == "complete" else None,
        "publicSolvers": [{"username": name, "normalizedUsername": name.strip().casefold(),
                           "sourceUserId": None, "certifiedAt": None, "sourceRank": i + 1}
                          for i, name in enumerate(solvers)],
    }


def cache(code="GC12345", **overrides):
    row = {"code": code, "name": "Puzzle", "owner": "Owner", "ownerKey": "owner",
           "geocacheType": 8, "region": "china", "city": "Shanghai", "cacheStatus": 1,
           "detailsUrl": f"https://coord.info/{code}", "finalCheckerId": "Final",
           "finalCertitudeUrl": "https://certitudes.org/Final", "completionRule": "single_final",
           "completionCheckerIds": ["Final"], "checkerSetCoverage": "complete"}
    row.update(overrides)
    return row


def manifest(count):
    return {"schemaVersion": 2, "datasetVersion": "v1", "generatedAt": "2026-11-01T00:00:00Z",
            "regions": list(REGIONS), "availabilities": list(AVAILABILITIES),
            "scopePathTemplate": "/data/certitude/versions/v1/scopes/{region}/{availability}.json",
            "cacheCount": count, "coverage": {}}


class CertitudeFactsTests(unittest.TestCase):
    def facts(self, c=None, checks=None):
        return build_cache_facts(c or cache(), checks or [])

    def dataset(self, facts=None):
        facts = facts or [self.facts(checks=[checker(solvers=["Solver"])])]
        m = manifest(len(facts))
        scopes = []
        for region in REGIONS:
            for availability in AVAILABILITIES:
                scope = build_scope(facts, region, availability)
                scope.update(schemaVersion=2, datasetVersion="v1", generatedAt=m["generatedAt"])
                scopes.append(scope)
        return m, scopes, {f["cache"]["code"]: f for f in facts}

    def guid_facts(self):
        owner_guid = "11111111-1111-4111-8111-111111111111"
        solver_guid = "22222222-2222-4222-8222-222222222222"
        owner_cache = cache(code="GC12345", owner="Owner", ownerGuid=owner_guid)
        final = checker(solvers=["Solver"])
        final["publicSolvers"][0]["guid"] = solver_guid
        return [self.facts(owner_cache, [final])], owner_guid, solver_guid

    def test_facts_are_internal_only_and_deduplicate_checker_ids_and_users(self):
        final = checker(solvers=[" Alice ", "alice"])
        final["publicSolvers"][0]["sourceUserId"] = "u1"
        final["publicSolvers"][1]["sourceUserId"] = "u1"
        fact = self.facts(checks=[final, checker("Other", solvers=["Other"]), checker("Final", solvers=["Ignored"])])
        self.assertEqual(set(fact), {"cache", "checkers", "stats", "certifiedSolvers"})
        self.assertEqual(len(fact["checkers"]), 2)
        self.assertEqual(fact["stats"]["observedCount"], 1)
        self.assertEqual(fact["certifiedSolvers"][0]["key"], "alice")
        self.assertNotIn("playerRecords", fact["cache"])

    def test_stage_only_solver_is_not_certified_but_known_final_is(self):
        f = self.facts(checks=[checker("Stage", "stage", ["StageUser"]), checker(solvers=["FinalUser"])])
        self.assertEqual([r["key"] for r in f["certifiedSolvers"]], ["finaluser"])
        self.assertEqual(f["stats"]["observedCount"], 1)

    def test_unknown_rule_suppresses_public_correct_count_but_keeps_attempt_facts(self):
        f = self.facts(cache(completionRule="unknown", completionCheckerIds=[]), [checker(solvers=["Solver"], success=3, failure=2)])
        self.assertFalse(f["stats"]["knownRule"])
        self.assertIsNone(f["stats"]["publicCorrectCount"])
        self.assertEqual(f["stats"]["attemptCount"], 5)

    def test_missing_and_zero_counter_pairs_keep_unknown_and_null_rate_semantics(self):
        missing = self.facts(checks=[checker(success=None, failure=2)])
        self.assertIsNone(missing["stats"]["attemptCount"])
        zero = self.facts(checks=[checker(success=0, failure=0)])
        self.assertEqual(zero["stats"]["attemptCount"], 0)
        self.assertIsNone(zero["stats"]["submissionRate"])

    def test_tied_and_unknown_metrics_have_stable_puzzle_order(self):
        facts = [self.facts(cache(code=code), [checker(success=success, failure=0)])
                 for code, success in (("GC33333", 2), ("GC11111", 1), ("GC22222", 1))]
        facts.append(self.facts(cache(code="GC44444"), [checker(success=None, failure=None)]))
        scope = build_scope(facts, "all", "all")
        rows = scope["puzzleRankings"]["attempts"]["asc"]
        self.assertEqual([r["code"] for r in rows], ["GC11111", "GC22222", "GC33333", "GC44444"])
        self.assertEqual([r["rank"] for r in rows], [1, 1, 3, None])

    def test_scope_filters_and_public_projection_exclude_internal_records(self):
        facts = [self.facts(cache(code="GC10001", cacheStatus=1)),
                 self.facts(cache(code="GC10002", cacheStatus=None)),
                 self.facts(cache(code="GC10003", cacheStatus=2))]
        active = build_scope(facts, "all", "active")
        self.assertEqual([r["code"] for r in active["caches"]], ["GC10001"])
        cache_row = active["caches"][0]
        self.assertNotIn("checkers", active)
        self.assertNotIn("certifiedSolvers", active)
        self.assertFalse({"playerRecords", "publicSolvers", "lookupCoverage"} & set(cache_row))

    def test_scope_graph_self_solve_unknown_owner_and_owner_directory(self):
        facts = [self.facts(cache(code="GC20001"), [checker(solvers=["Owner", "Solver"])])]
        facts.append(self.facts(cache(code="GC20002", owner=None, ownerKey=None), [checker(solvers=["Solver"]) ]))
        scope = build_scope(facts, "all", "all")
        self.assertFalse(any(e["ownerKey"] == e["solverKey"] for e in scope["graph"]["edges"]))
        self.assertEqual(len(scope["graph"]["edges"]), 1)
        self.assertIn("owner", [p["key"] for p in scope["graph"]["players"]])

    def test_all_scope_rankings_cover_full_cache_set(self):
        facts = [self.facts(cache(code=f"GC{i:05d}"), [checker(solvers=[f"S{i}"])]) for i in range(1, 5)]
        scope = build_scope(facts, "all", "all")
        self.assertEqual(len(scope["rankings"]["solvers"]), 4)
        self.assertEqual({r["code"] for r in scope["puzzleRankings"]["correct"]["asc"]},
                         {f["cache"]["code"] for f in facts})

    def test_validator_accepts_schema2_dataset_and_rejects_bad_versions_counts_and_ranks(self):
        m, scopes, facts = self.dataset()
        self.assertTrue(validate_dataset(m, scopes, facts))
        for field, value in (("schemaVersion", 1), ("scopePathTemplate", "/wrong")):
            bad = dict(m)
            bad[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate_dataset(bad, scopes, facts)
        bad_scopes = [dict(s) for s in scopes]
        bad_scopes[0]["summary"] = dict(bad_scopes[0]["summary"], cacheCount=99)
        with self.assertRaises(ValueError):
            validate_dataset(m, bad_scopes, facts)

    def test_validator_rejects_forged_internal_facts_and_scope_extras(self):
        m, scopes, facts = self.dataset()
        facts["GC12345"]["stats"]["observedCount"] = 42
        with self.assertRaises(ValueError):
            validate_dataset(m, scopes, facts)
        m, scopes, facts = self.dataset()
        scopes[0]["caches"][0]["playerRecords"] = {}
        self.assertTrue(validate_dataset(m, scopes, facts))

    def test_v141_projection_and_legacy_extras(self):
        m, scopes, facts = self.dataset()
        m.pop("cacheCount")
        m.pop("coverage")
        self.assertTrue(validate_dataset(m, scopes, facts))
        scope = scopes[0]
        cache_row = scope["caches"][0]
        self.assertEqual(set(cache_row), {"code", "name", "owner", "geocacheType", "region", "cacheStatus", "detailsUrl", "finalCertitudeUrl", "stats"})
        self.assertEqual(set(cache_row["stats"]), {"publicCorrectCount", "successCount", "failureCount", "attemptCount", "submissionRate"})
        self.assertNotIn("certificationCount", scope["rankings"]["owners"][0])
        edge = scope["graph"]["edges"][0]
        self.assertEqual(set(edge), {"ownerKey", "ownerName", "solverKey", "solverName", "count"})
        m.update(cacheCount=-1, coverage="invalid", cachePathTemplate="legacy")
        scope["coverage"] = "invalid"
        cache_row.update(city=2, ownerKey="wrong", finalCheckerId=[])
        cache_row["stats"].update(knownRule="invalid", observedCount=-1, correctCount="invalid", counterCoverage=[])
        scope["rankings"]["owners"][0]["certificationCount"] = -1
        edge["cacheCodes"] = ["GC99999"]
        self.assertTrue(validate_dataset(m, scopes, facts))

    def test_summary_certification_count_is_distinct_gc_solver_facts(self):
        checks = [checker(solvers=["Solver"]), checker("Second", solvers=["Solver"])]
        f = self.facts(checks=checks)
        self.assertEqual(f["stats"]["publicCorrectCount"], 1)
        scope = build_scope([f], "all", "all")
        self.assertEqual(scope["summary"]["certificationCount"], 1)

    def test_guid_metadata_projects_to_owner_solver_and_directory_without_changing_counts(self):
        facts, owner_guid, solver_guid = self.guid_facts()
        fact = facts[0]
        self.assertEqual(fact["certifiedSolvers"][0]["guid"], solver_guid)
        rebuilt = build_cache_facts(fact["cache"], fact["checkers"])
        self.assertEqual(rebuilt["certifiedSolvers"], fact["certifiedSolvers"])
        scope = build_scope(facts, "all", "all")
        self.assertEqual(scope["summary"]["certificationCount"], 1)
        self.assertEqual(scope["rankings"]["owners"][0]["guid"], owner_guid)
        self.assertEqual(scope["rankings"]["solvers"][0]["guid"], solver_guid)
        self.assertEqual({p["guid"] for p in scope["graph"]["players"]}, {owner_guid, solver_guid})

    def test_guid_user_with_both_roles_requires_consistent_metadata(self):
        guid = "11111111-1111-4111-8111-111111111111"
        final = checker(solvers=["Solver"])
        owner_solver = checker(solvers=["Owner"])
        owner_solver["publicSolvers"][0]["guid"] = guid
        facts = [self.facts(cache(ownerGuid=guid), [final]),
                 self.facts(cache(code="GC12346", owner="Other"), [owner_solver])]
        scope = build_scope(facts, "all", "all")
        owner = next(row for row in scope["rankings"]["owners"] if row["key"] == "owner")
        solver = next(row for row in scope["rankings"]["solvers"] if row["key"] == "owner")
        player = next(row for row in scope["graph"]["players"] if row["key"] == "owner")
        self.assertEqual((owner["guid"], solver["guid"], player["guid"]), (guid, guid, guid))
        facts[1]["certifiedSolvers"][0]["guid"] = "33333333-3333-4333-8333-333333333333"
        with self.assertRaises(ValueError):
            build_scope(facts, "all", "all")

    def test_guid_optional_null_and_legacy_omission_are_accepted(self):
        m, scopes, facts = self.dataset()
        self.assertTrue(validate_dataset(m, scopes, facts))
        for scope in scopes:
            for kind in ("solvers", "owners"):
                for row in scope["rankings"][kind]:
                    row.pop("guid", None)
            for player in scope["graph"]["players"]:
                player.pop("guid", None)
        self.assertTrue(validate_dataset(m, scopes, facts))

    def test_guid_projection_rejects_malformed_values_and_fact_mismatch(self):
        m, scopes, facts = self.dataset(self.guid_facts()[0])
        self.assertTrue(validate_dataset(m, scopes, facts))
        scopes[0]["rankings"]["solvers"][0]["guid"] = "not-a-uuid"
        with self.assertRaises(ValueError):
            validate_dataset(m, scopes, facts)
        m, scopes, facts = self.dataset(self.guid_facts()[0])
        scopes[0]["rankings"]["solvers"][0].pop("guid")
        scopes[0]["graph"]["players"][1].pop("guid")
        with self.assertRaises(ValueError):
            validate_dataset(m, scopes, facts)

    def test_conflicting_same_key_guid_across_caches_is_rejected(self):
        facts, _, solver_guid = self.guid_facts()
        second = self.facts(cache(code="GC12346", owner="DifferentOwner"),
                            [checker(solvers=["Solver"])])
        second["certifiedSolvers"][0]["guid"] = "33333333-3333-4333-8333-333333333333"
        with self.assertRaises(ValueError):
            build_scope(facts + [second], "all", "all")

    def test_unknown_rule_and_partial_solver_list_preserve_available_correct_count(self):
        c = cache(completionRule="unknown", completionCheckerIds=[])
        f = self.facts(c, [checker(solvers=["Solver"], coverage="partial")])
        scope = build_scope([f], "all", "all")
        self.assertIsNone(scope["caches"][0]["stats"]["publicCorrectCount"])
        c = cache()
        f = self.facts(c, [checker(solvers=["Solver"], coverage="partial")])
        scope = build_scope([f], "all", "all")
        self.assertEqual(scope["caches"][0]["stats"]["publicCorrectCount"], 1)

    def test_validator_rejects_corrupt_required_facts_without_source_facts(self):
        mutations = (
            lambda s: s[0]["caches"][0]["stats"].update(successCount=4),
            lambda s: s[0]["caches"][0].update(region="Mars"),
            lambda s: s[0]["rankings"]["solvers"][0].update(rank=True),
            lambda s: s[0]["graph"]["edges"][0].update(count=7),
            lambda s: s[0]["rankings"]["owners"][0].update(cacheCodes=[]),
            lambda s: s[0]["graph"]["order"].update(owners=[]),
            lambda s: s[0]["caches"][0]["stats"].update(publicCorrectCount=None),
        )
        for index, mutate in enumerate(mutations):
            m, scopes, _ = self.dataset()
            mutate(scopes)
            with self.subTest(index=index), self.assertRaises(ValueError):
                validate_dataset(m, scopes)

    def test_case_variant_names_across_filtered_scopes_validate(self):
        facts = [self.facts(cache(code="GC11111", region="china"), [checker(solvers=["Bob"])]),
                 self.facts(cache(code="GC22222", region="taiwan"), [checker(solvers=["BOB"])])]
        m = manifest(2)
        scopes = []
        for region in REGIONS:
            for availability in AVAILABILITIES:
                scope = build_scope(facts, region, availability)
                scope.update(schemaVersion=2, datasetVersion="v1", generatedAt=m["generatedAt"])
                scopes.append(scope)
        self.assertTrue(validate_dataset(m, scopes, {f["cache"]["code"]: f for f in facts}))

    def test_case_variant_owner_solver_display_names_validate(self):
        facts = [self.facts(cache(code="GC30001", owner="Alice", ownerKey="discarded"),
                            [checker(solvers=["Bob"])]),
                 self.facts(cache(code="GC30002", owner="Other"),
                            [checker(solvers=["alice"])])]
        m = manifest(2)
        scopes = []
        for region in REGIONS:
            for availability in AVAILABILITIES:
                scope = build_scope(facts, region, availability)
                scope.update(schemaVersion=2, datasetVersion="v1", generatedAt=m["generatedAt"])
                scopes.append(scope)
        self.assertTrue(validate_dataset(m, scopes, {f["cache"]["code"]: f for f in facts}))
        all_scope = scopes[0]
        self.assertEqual(next(row for row in all_scope["rankings"]["owners"] if row["key"] == "alice")["username"], "Alice")
        self.assertEqual(next(row for row in all_scope["rankings"]["solvers"] if row["key"] == "alice")["username"], "alice")


if __name__ == "__main__":
    unittest.main()
