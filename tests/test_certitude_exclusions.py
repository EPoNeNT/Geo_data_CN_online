"""Offline checks for explicit Certitude parent-cache exclusions."""
import json
from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import generate_certitude
from generate_certitude import build_dataset, load_exclusions, load_inputs, publish_dataset


class CertitudeExclusionTests(unittest.TestCase):
    def write_config(self, directory, content):
        path = Path(directory) / "rules.json"
        path.write_text(content, encoding="utf-8")
        return path

    def test_default_configuration_excludes_reviewed_parent(self):
        self.assertIn("GC721K1", load_exclusions())
        manifest, scopes, facts = build_dataset(
            [{"code": "GC721K1", "country": "China"}],
            {"GC721K1": {"checkerId": "GC721K1", "cacheCode": "GC721K1",
                          "mappingStatus": "verified", "role": "final"}},
        )
        self.assertEqual(len(scopes), 15)
        self.assertNotIn("GC721K1", facts)

    def test_loader_normalizes_codes_and_is_independent_of_working_directory(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as temporary:
            path = self.write_config(temporary, '{" gcabc123 ": "  reviewed  "}')
            with patch.object(generate_certitude, "EXCLUSIONS_PATH", path):
                original = Path.cwd()
                try:
                    # The default path is module-relative, not process-cwd-relative.
                    import os
                    os.chdir(temporary)
                    self.assertEqual(load_exclusions(), {"GCABC123": "reviewed"})
                finally:
                    os.chdir(original)

    def test_loader_rejects_missing_invalid_non_object_and_invalid_entries(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as temporary:
            missing = Path(temporary) / "missing.json"
            with self.assertRaises(FileNotFoundError):
                load_exclusions(missing)
            cases = (
                "{",
                "[]",
                '{"not-a-gc": "reason"}',
                '{"GC123": "  "}',
                '{"GC123": null}',
                '{"GC123": "first", "GC123": "second"}',
                '{" gc123 ": "first", "GC123": "second"}',
            )
            for content in cases:
                path = self.write_config(temporary, content)
                with self.subTest(content=content), self.assertRaises((ValueError, json.JSONDecodeError)):
                    load_exclusions(path)

    def test_filter_uses_parent_candidate_code_and_removes_parent_everywhere(self):
        candidates = [
            {"code": "GC721K1", "country": "China", "owner_username": "Excluded owner",
             "cache_status": 1},
            {"code": "GC10001", "country": "China", "owner_username": "Zero owner",
             "cache_status": 1},
            {"code": "GC10002", "country": "China", "owner_username": "Active owner",
             "cache_status": 1},
        ]
        checkers = {
            "unrelated-checker-id": {"checkerId": "unrelated-checker-id", "cacheCode": "GC721K1",
                "mappingStatus": "verified", "role": "final", "sourceUrl": "excluded",
                "successCount": 3, "failureCount": 4,
                "publicSolvers": [{"username": "Excluded solver"}]},
            "GC10001": {"checkerId": "GC10001", "cacheCode": "GC10001",
                "mappingStatus": "verified", "role": "final", "sourceUrl": "zero",
                "counterObservedAt": "2026-11-01T00:00:00Z",
                "successCount": 0, "failureCount": 0, "publicSolvers": []},
            "GC10002": {"checkerId": "GC10002", "cacheCode": "GC10002",
                "mappingStatus": "verified", "role": "final", "sourceUrl": "active",
                "counterObservedAt": "2026-11-01T00:00:00Z",
                "successCount": 2, "failureCount": 1,
                "publicSolvers": [{"username": "Retained solver"}]},
        }
        users = [{"user_name": "Excluded owner", "guid": "bad"}]
        original = deepcopy((candidates, checkers, users))
        manifest, scopes, facts = build_dataset(candidates, checkers, users)

        self.assertEqual((candidates, checkers, users), original)
        self.assertNotIn("GC721K1", facts)
        self.assertEqual(set(facts), {"GC10001", "GC10002"})
        self.assertEqual(facts["GC10001"]["stats"]["attemptCount"], 0)
        self.assertEqual(facts["GC10002"]["stats"]["attemptCount"], 3)
        self.assertEqual(len(scopes), 15)
        serialized_scopes = json.dumps(scopes, ensure_ascii=False)
        for forbidden in ("GC721K1", "Excluded owner", "Excluded solver", "excluded"):
            self.assertNotIn(forbidden, serialized_scopes)
        all_scope = next(scope for scope in scopes
                         if scope["scope"] == {"region": "all", "availability": "all"})
        self.assertIn("GC10001", [row["code"] for row in all_scope["caches"]])
        self.assertEqual(all_scope["summary"], {"cacheCount": 2, "ownerCount": 2,
                                                "solverCount": 1, "certificationCount": 1})
        self.assertEqual([(edge["ownerKey"], edge["solverKey"], edge["count"])
                          for edge in all_scope["graph"]["edges"]],
                         [("active owner", "retained solver", 1)])
        self.assertEqual(manifest["schemaVersion"], 2)

    def test_audit_exclusions_filter_parents_and_keep_explicit_rules_authoritative(self):
        candidates = [
            {"code": "GC721K1", "country": "China", "owner_username": "Manual"},
            {"code": "GC20001", "country": "China", "owner_username": "Automatic"},
            {"code": "GC20002", "country": "China", "owner_username": "Retained"},
        ]
        checkers = {code: {"checkerId": code, "cacheCode": code, "mappingStatus": "verified",
                          "role": "final", "sourceUrl": "https://certitudes.org/" + code,
                          "successCount": 0, "failureCount": 0,
                          "publicSolvers": []} for code in ("GC721K1", "GC20001", "GC20002")}
        _, scopes, facts = build_dataset(candidates, checkers, None,
                                         {"gc20001": "  confirmed not linked  ", "GC721K1": "automatic reason"})
        self.assertEqual(set(facts), {"GC20002"})
        self.assertEqual(len(scopes), 15)
        all_scope = next(scope for scope in scopes if scope["scope"] == {"region": "all", "availability": "all"})
        self.assertEqual(all_scope["summary"]["cacheCount"], 1)
        # With no active automatic result, the configured manual exclusion still applies.
        self.assertNotIn("GC721K1", build_dataset(candidates, checkers, None, {})[2])

    def test_audit_exclusion_map_rejects_invalid_and_normalized_duplicate_entries(self):
        candidates = [{"code": "GC1", "country": "China"}]
        with self.assertRaises(ValueError):
            build_dataset(candidates, {}, None, {"not-a-code": "reason"})
        with self.assertRaises(ValueError):
            build_dataset(candidates, {}, None, {"gc1": "one", "GC1": "two"})

    def test_applied_audit_records_require_valid_status_time_and_reason(self):
        class Cursor:
            def __init__(self): self.queries = []
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def execute(self, query, params=None): self.queries.append(query)
            def fetchall(self): return self.rows.pop(0)
        class Connection:
            def __init__(self, rows): self.cur = Cursor(); self.cur.rows = rows
            def set_session(self, **kwargs): pass
            def cursor(self): return self.cur
        valid_audit = {"autoExcluded": True, "lastValidStatus": "not_linked",
                       "checkedAt": "2026-11-02T00:00:00Z", "autoExclusionReason": "not linked"}
        for audit in (
            {**valid_audit, "lastValidStatus": "linked"},
            {**valid_audit, "checkedAt": "yesterday"},
            {**valid_audit, "checkedAt": "2026-11-02T00:00:00+08:00"},
            {**valid_audit, "autoExclusionReason": "  "},
        ):
            connection = Connection([[ ], [ ], [("GC1", audit)]])
            with patch("generate_certitude.load_candidates", return_value=[]), self.assertRaises(ValueError):
                load_inputs(connection)
        connection = Connection([[ ], [ ], [("GC1", valid_audit)]])
        with patch("generate_certitude.load_candidates", return_value=[]):
            self.assertEqual(load_inputs(connection), ([], {}, [], {"GC1": "not linked"}))

    def test_removing_rule_restores_parent_and_bad_config_preserves_manifest(self):
        candidate = [{"code": "GC721K1", "country": "China", "cache_status": 1}]
        checker = {"GC721K1": {"checkerId": "GC721K1", "cacheCode": "GC721K1",
                              "mappingStatus": "verified", "role": "final",
                              "sourceUrl": "https://certitudes.org/GC721K1",
                              "counterObservedAt": "2026-11-01T00:00:00Z",
                              "successCount": 0, "failureCount": 0, "publicSolvers": []}}
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as temporary:
            config = self.write_config(temporary, "{}")
            output = Path(temporary) / "published"
            with patch.object(generate_certitude, "EXCLUSIONS_PATH", config):
                first = build_dataset(candidate, checker)
                publish_dataset(*first, output_dir=output)
                old_manifest = (output / "manifest.json").read_bytes()
                restored = build_dataset(candidate, checker)
                self.assertIn("GC721K1", restored[2])
                config.write_text("[]", encoding="utf-8")
                with self.assertRaises(ValueError):
                    build_dataset(candidate, checker)
                self.assertEqual((output / "manifest.json").read_bytes(), old_manifest)


if __name__ == "__main__":
    unittest.main()
