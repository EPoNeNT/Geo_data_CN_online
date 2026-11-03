"""Offline policy checks: saved database observations remain publishable."""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class GenerationCollectionPolicyTests(unittest.TestCase):
    def setUp(self):
        workflow = (ROOT / ".github/workflows/crawl.yml").read_text(encoding="utf-8")
        self.generator = workflow.split("  generate-data:\n", 1)[1]

    def test_registration_enrichment_failure_does_not_block_reports(self):
        step = self.generator.split("- name: Crawl missing user registration dates", 1)[1].split("- name:", 1)[0]
        self.assertIn("continue-on-error: true", step)

    def test_certitude_generation_is_independent_of_collection_outcome(self):
        step = self.generator.split("- name: Generate Certitude static data", 1)[1].split("- name:", 1)[0]
        self.assertIn("if: success()", step)
        self.assertNotIn("needs.crawl-certitude.result", step)


if __name__ == "__main__":
    unittest.main()
