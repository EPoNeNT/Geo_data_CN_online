"""Verify that static publication contains only the data used by summary views."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from generate_certitude import build_dataset, load_inputs, publish_dataset


class SummaryOutputTests(unittest.TestCase):
    def test_publication_has_fixed_file_count_without_per_cache_or_detail_payloads(self):
        stamp = "2026-11-01T00:00:00Z"
        checker = {"checkerId": "GC1", "sourceUrl": "https://www.certitudes.org/certitude?wp=GC1",
                   "cacheCode": "GC1", "mappingStatus": "verified", "role": "final",
                   "successCount": 1, "failureCount": 2, "counterObservedAt": stamp,
                   "publicSolvers": [{"username": "Solver", "certifiedAt": stamp}],
                   "solverListCoverage": "complete", "solverListObservedAt": stamp,
                   "crawl": {"status": "ok", "lastAttemptAt": stamp}}
        batch = build_dataset([{"code": "GC1", "country": "China", "owner_username": "Owner"}], {"GC1": checker})
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as temporary:
            root = Path(temporary)
            destination = publish_dataset(*batch, output_dir=root)
            files = list(root.rglob("*.json"))
            self.assertEqual(len(files), 16)
            self.assertFalse((destination / "caches").exists())
            manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["schemaVersion"], 2)
            self.assertEqual(set(manifest), {"schemaVersion", "datasetVersion", "generatedAt",
                                             "regions", "availabilities", "scopePathTemplate"})
            combined = "".join(path.read_text(encoding="utf-8") for path in files)
            for field in ("publicSolvers", "playerRecords", "playerOptions", "lookupCoverage", "certifiedSolvers", "checkers"):
                self.assertNotIn('"' + field + '":', combined)
            scope = json.loads((destination / "scopes/all/all.json").read_text(encoding="utf-8"))
            self.assertEqual(scope["summary"]["certificationCount"], 1)
            self.assertEqual(scope["graph"]["edges"][0]["count"], 1)
            self.assertNotIn("coverage", scope)
            self.assertEqual(set(scope["caches"][0]), {"code", "name", "owner", "geocacheType", "region",
                                                      "cacheStatus", "detailsUrl", "finalCertitudeUrl", "stats"})
            self.assertEqual(set(scope["caches"][0]["stats"]), {"publicCorrectCount", "successCount",
                                                               "failureCount", "attemptCount", "submissionRate"})
            self.assertNotIn("certificationCount", scope["rankings"]["owners"][0])
            self.assertNotIn("cacheCodes", scope["graph"]["edges"][0])
            self.assertEqual(scope["rankings"]["solvers"][0]["cacheCodes"], ["GC1"])
            self.assertEqual(scope["rankings"]["owners"][0]["cacheCodes"], ["GC1"])

    def test_generation_only_reads_inputs_needed_by_summaries(self):
        class Cursor:
            def __init__(self): self.queries = []
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def execute(self, query, params=None): self.queries.append(query)
            def fetchall(self): return []
        class Connection:
            def __init__(self): self.cur = Cursor()
            def set_session(self, **kwargs): pass
            def cursor(self): return self.cur
        connection = Connection()
        with patch("generate_certitude.load_candidates", return_value=[]):
            inputs = load_inputs(connection)
        self.assertEqual(inputs, ([], {}))
        self.assertEqual(connection.cur.queries, ["SELECT checker_id, record FROM certitude_checkers"])


if __name__ == "__main__":
    unittest.main()
