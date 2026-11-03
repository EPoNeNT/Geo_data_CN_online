import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from static_publication import publish_json_reports


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("production_generate_data", ROOT / "generate_data.py")
generator_module = importlib.util.module_from_spec(spec)
with mock.patch.dict(os.environ, {
    "DATABASE_URL": "postgresql://unused:unused@localhost/unused",
    "LOG_TO_FILE": "0",
}):
    spec.loader.exec_module(generator_module)


class StaticPublicationTests(unittest.TestCase):
    names = ["overview.json", "player-rankings.json", "cache-rankings.json", "city-rankings.json", "generated-at.json"]

    def _reports(self, timestamp="2026-11-03T00:00:00.000Z"):
        return [(name, lambda name=name: {"name": name, "generatedAt": timestamp}) for name in self.names]

    def test_success_publishes_all_reports_with_shared_timestamp(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "tests") as directory:
            publish_json_reports(directory, self._reports())
            documents = [json.loads((Path(directory) / name).read_text(encoding="utf-8")) for name in self.names]
            self.assertEqual({doc["generatedAt"] for doc in documents}, {"2026-11-03T00:00:00.000Z"})

    def test_generate_all_succeeds_from_empty_output_and_shares_timestamp(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "tests") as directory:
            generator = generator_module.DataGenerator.__new__(generator_module.DataGenerator)
            generator._generation_timestamp = None
            generator.ensure_output_dir = lambda: None
            for filename in self.names:
                method = "generate_" + filename.removesuffix(".json").replace("-", "_") + "_json"
                setattr(generator, method, lambda filename=filename: {"generatedAt": generator.get_generated_at(), "file": filename})
            generator.generate_timestamp_json = lambda: {"generatedAt": generator.get_generated_at(), "version": "1"}
            with mock.patch.object(generator_module, "OUTPUT_DIR", directory):
                generated, failed = generator.generate_all()
            self.assertEqual(generated, self.names)
            self.assertEqual(failed, [])
            documents = [json.loads((Path(directory) / name).read_text(encoding="utf-8")) for name in self.names]
            self.assertEqual(len({doc["generatedAt"] for doc in documents}), 1)

    def test_middle_generator_failure_preserves_existing_outputs(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "tests") as directory:
            output = Path(directory)
            for name in self.names:
                (output / name).write_text('{"old": true}', encoding="utf-8")
            reports = self._reports()
            reports[2] = (reports[2][0], lambda: (_ for _ in ()).throw(RuntimeError("injected")))
            with self.assertRaisesRegex(RuntimeError, "injected"):
                publish_json_reports(directory, reports)
            self.assertTrue(all(json.loads((output / name).read_text(encoding="utf-8")) == {"old": True} for name in self.names))

    def test_empty_output_and_invalid_number(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "tests") as directory:
            with self.assertRaises(ValueError):
                publish_json_reports(directory, [("bad.json", lambda: {"generatedAt": "2026-11-03T00:00:00.000Z", "value": float("nan")})])
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_invalid_timestamp_is_rejected(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "tests") as directory:
            with self.assertRaisesRegex(ValueError, "generatedAt"):
                publish_json_reports(directory, [("bad.json", lambda: {"generatedAt": "now"})])

    def test_replace_failure_rolls_back_already_replaced_files(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "tests") as directory:
            output = Path(directory)
            for name in self.names:
                (output / name).write_text('{"old": true}', encoding="utf-8")
            real_replace = os.replace
            replace_count = 0

            def failing_replace(source, target):
                nonlocal replace_count
                replace_count += 1
                if replace_count == 3:
                    raise OSError("injected publication failure")
                return real_replace(source, target)

            with mock.patch("static_publication.os.replace", side_effect=failing_replace):
                with self.assertRaisesRegex(OSError, "injected publication failure"):
                    publish_json_reports(directory, self._reports())
            self.assertTrue(all(json.loads((output / name).read_text(encoding="utf-8")) == {"old": True} for name in self.names))

    def test_incomplete_rollback_retains_recovery_files(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "tests") as directory:
            output = Path(directory)
            for name in self.names:
                (output / name).write_text('{"old": true}', encoding="utf-8")
            real_replace = os.replace
            replace_count = 0

            def failing_replace(source, target):
                nonlocal replace_count
                replace_count += 1
                if replace_count in {2, 3}:
                    raise OSError("injected replace error")
                return real_replace(source, target)

            with mock.patch("static_publication.os.replace", side_effect=failing_replace):
                with self.assertRaisesRegex(RuntimeError, "recovery files retained at") as raised:
                    publish_json_reports(directory, self._reports())
            recovery_dir = Path(str(raised.exception).split("recovery files retained at ", 1)[1])
            self.assertTrue((recovery_dir / "overview.json.backup").exists())
            self.assertTrue((output / "overview.json").exists())

    def test_generator_failure_is_reported_by_main(self):
        class FailedGenerator:
            def __init__(self, _database_url):
                pass

            def connect(self):
                pass

            def generate_all(self):
                return ["overview.json"], ["player-rankings.json"]

            def close(self):
                pass

        with mock.patch.object(generator_module, "DataGenerator", FailedGenerator):
            with self.assertRaisesRegex(RuntimeError, "player-rankings.json"):
                generator_module.main()


if __name__ == "__main__":
    unittest.main()
