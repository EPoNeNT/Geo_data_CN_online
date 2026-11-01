"""Offline checks for candidate discovery and atomic Certitude publication."""
import json
from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from generate_certitude import build_dataset, publish_dataset


class PipelineTests(unittest.TestCase):
    def test_empty_dataset_has_fifteen_scopes_and_unique_version(self):
        first = build_dataset([], {})
        second = build_dataset([], {})
        self.assertEqual(len(first[1]), 15)
        self.assertNotEqual(first[0]["datasetVersion"], second[0]["datasetVersion"])

    def test_prior_verified_mapping_conflict_retains_cache_with_unknown_stats(self):
        candidates = [{"code": "GC1", "country": "China"}]
        checkers = {"GC1": {"checkerId": "GC1", "cacheCode": "GC1", "mappingStatus": "conflict",
                            "mappingEvidence": "previous parent GC1; new parent GC2", "role": "unknown"}}
        manifest, scopes, details = build_dataset(candidates, checkers)
        self.assertEqual(scopes[0]["summary"]["cacheCount"], 1)
        self.assertFalse(details["GC1"]["stats"]["knownRule"])
        self.assertIsNone(details["GC1"]["stats"]["publicCorrectCount"])

    def test_generation_uses_saved_observations_without_modifying_source_records(self):
        candidates = [{"code": "GC1", "country": "China"}]
        old = "2000-01-01T00:00:00Z"
        checkers = {"GC1": {"checkerId": "GC1", "sourceUrl": "https://www.certitudes.org/certitude?wp=GC1",
                            "cacheCode": "GC1", "mappingStatus": "verified", "role": "final",
                            "successCount": 0, "failureCount": 0, "counterObservedAt": old,
                            "publicSolvers": [], "solverListCoverage": "complete", "solverListObservedAt": old,
                            "crawl": {"status": "ok", "stale": False}}}
        original = deepcopy(checkers)
        _, scopes, facts = build_dataset(candidates, checkers)
        self.assertEqual(checkers, original)
        self.assertEqual(facts["GC1"]["stats"]["attemptCount"], 0)
        self.assertNotIn("checkers", scopes[0]["caches"][0])

    def test_failed_validation_keeps_old_manifest_and_old_version(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as temp:
            root = Path(temp) / "certitude"
            first = build_dataset([], {})
            publish_dataset(*first, output_dir=root)
            original = (root / "manifest.json").read_bytes()
            broken = build_dataset([], {})
            broken[1][0]["summary"]["cacheCount"] = 9
            with self.assertRaises(ValueError):
                publish_dataset(*broken, output_dir=root)
            self.assertEqual((root / "manifest.json").read_bytes(), original)
            second = build_dataset([], {})
            publish_dataset(*second, output_dir=root)
            self.assertTrue((root / "versions" / first[0]["datasetVersion"]).is_dir())
            self.assertEqual(json.loads((root / "manifest.json").read_text())["datasetVersion"], second[0]["datasetVersion"])


if __name__ == "__main__":
    unittest.main()
