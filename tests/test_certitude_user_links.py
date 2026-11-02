"""Existing database identities enrich summaries without changing certification facts."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from generate_certitude import build_dataset, load_inputs, publish_dataset
from tests.test_certitude_data import checker

SOLVER_GUID = "11111111-1111-4111-8111-111111111111"
OWNER_GUID = "22222222-2222-4222-8222-222222222222"


class ExistingUserLinkTests(unittest.TestCase):
    def inputs(self):
        candidates = [{"code": "GC1", "country": "China", "owner_username": "owner", "cache_status": 0}]
        record = checker(solvers=[" solver ", "SOLVER", "Unknown"])
        record.update(cacheCode="GC1", mappingStatus="verified")
        users = [{"user_name": "Owner", "guid": OWNER_GUID},
                 {"user_name": "Solver", "guid": SOLVER_GUID}]
        return candidates, {record["checkerId"]: record}, users

    def test_canonical_names_and_guids_reach_rankings_directory_and_published_json(self):
        inputs = self.inputs()
        before = deepcopy(inputs)
        batch = build_dataset(*inputs)
        scope = batch[1][0]
        solver = next(r for r in scope["rankings"]["solvers"] if r["key"] == "solver")
        self.assertEqual((solver["username"], solver["guid"], solver["solvedCount"]), ("Solver", SOLVER_GUID, 1))
        self.assertEqual(scope["rankings"]["owners"][0]["guid"], OWNER_GUID)
        self.assertEqual(scope["caches"][0]["owner"], "Owner")
        players = {p["key"]: p for p in scope["graph"]["players"]}
        self.assertEqual(players["solver"]["guid"], SOLVER_GUID)
        self.assertEqual(players["owner"]["guid"], OWNER_GUID)
        self.assertEqual(players["unknown"]["username"], "Unknown")
        self.assertIsNone(players["unknown"]["guid"])
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as temporary:
            destination = publish_dataset(*batch, output_dir=temporary)
            public = json.loads((destination / "scopes/all/all.json").read_text(encoding="utf-8"))
            self.assertEqual(next(p for p in public["graph"]["players"] if p["key"] == "solver")["guid"], SOLVER_GUID)
            self.assertEqual(len(list(Path(temporary).rglob("*.json"))), 16)
        self.assertEqual(inputs, before)

    def test_ambiguous_names_never_pick_one_database_identity(self):
        candidates, records, users = self.inputs()
        users.append({"user_name": "sOLVER", "guid": OWNER_GUID})
        _, scopes, _ = build_dataset(candidates, records, users)
        player = next(p for p in scopes[0]["graph"]["players"] if p["key"] == "solver")
        self.assertIsNone(player["guid"])
        self.assertNotEqual(player["username"], "Solver")

    def test_missing_or_invalid_database_guid_keeps_canonical_name_and_null_guid(self):
        for guid in (None, "", "no guid"):
            candidates, records, _ = self.inputs()
            _, scopes, _ = build_dataset(candidates, records, [{"user_name": "Solver", "guid": guid}])
            player = next(p for p in scopes[0]["graph"]["players"] if p["key"] == "solver")
            with self.subTest(guid=guid):
                self.assertEqual(player["username"], "Solver")
                self.assertIsNone(player["guid"])

    def test_user_links_do_not_change_counts_ranks_or_cache_associations_in_any_scope(self):
        candidates, records, users = self.inputs()
        original = build_dataset(candidates, records)[1]
        linked = build_dataset(candidates, records, users)[1]
        for before, after in zip(original, linked):
            with self.subTest(scope=before["scope"]):
                self.assertEqual(before["summary"], after["summary"])
                self.assertEqual(before["puzzleRankings"], after["puzzleRankings"])
                self.assertEqual([c["stats"] for c in before["caches"]], [c["stats"] for c in after["caches"]])
                for kind in ("solvers", "owners"):
                    self.assertEqual([{k: v for k, v in r.items() if k not in ("guid", "username")}
                                      for r in before["rankings"][kind]],
                                     [{k: v for k, v in r.items() if k not in ("guid", "username")}
                                      for r in after["rankings"][kind]])
                self.assertEqual([(e["ownerKey"], e["solverKey"], e["count"]) for e in before["graph"]["edges"]],
                                 [(e["ownerKey"], e["solverKey"], e["count"]) for e in after["graph"]["edges"]])

    def test_source_user_id_is_not_exported_as_a_geocaching_guid(self):
        candidates, records, _ = self.inputs()
        records["Final"]["publicSolvers"] = [{"username": "Unknown", "sourceUserId": SOLVER_GUID}]
        _, scopes, _ = build_dataset(candidates, records)
        player = next(p for p in scopes[0]["graph"]["players"] if p["key"] == "unknown")
        self.assertIsNone(player["guid"])

    def test_load_uses_one_readonly_transaction_and_only_selects_existing_users(self):
        from unittest.mock import MagicMock
        connection = MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.fetchall.side_effect = [[("Final", {"checkerId": "Final"})], [("Solver", SOLVER_GUID)], []]
        with patch("generate_certitude.load_candidates", return_value=[]):
            inputs = load_inputs(connection)
        connection.set_session.assert_called_once_with(isolation_level="REPEATABLE READ", readonly=True)
        self.assertEqual(inputs[2], [{"user_name": "Solver", "guid": SOLVER_GUID}])
        self.assertEqual(inputs[3], {})
        self.assertEqual([call.args[0] for call in cursor.execute.call_args_list],
                         ["SELECT checker_id, record FROM certitude_checkers", 'SELECT user_name, guid FROM "user"',
                          "SELECT cache_code, record->'usageAudit' FROM certitude_descriptions "
                          "WHERE record->'usageAudit'->'autoExcluded' = 'true'::jsonb"])


if __name__ == "__main__":
    unittest.main()
