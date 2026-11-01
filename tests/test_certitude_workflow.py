import unittest
from pathlib import Path

try:
    import yaml
except ImportError:
    yaml = None

WORKFLOW = Path('.github/workflows/crawl.yml')
TEXT = WORKFLOW.read_text(encoding='utf-8')

class CertitudeWorkflowTests(unittest.TestCase):
    def test_yaml_parses_when_pyyaml_available(self):
        if yaml is None:
            self.skipTest('PyYAML is not installed')
        data = yaml.safe_load(TEXT)
        workflow_dispatch = data.get('on', data.get(True, {}))
        self.assertIn('workflow_dispatch', workflow_dispatch)
        self.assertIn('generate-data', data['jobs'])

    def test_dispatch_has_certitude_options(self):
        self.assertIn('certitude', TEXT)
        self.assertIn('description_limit', TEXT)

    def test_job_runs_certitude_and_preserves_static_deploy_paths(self):
        self.assertIn("run: python -m unittest discover -s tests -p 'test_certitude*.py'", TEXT)
        self.assertIn('python crawl_certitude.py --description-limit "$CERTITUDE_DESCRIPTION_LIMIT"', TEXT)
        self.assertIn('python generate_certitude.py', TEXT)
        self.assertIn('cp public/data/*.json /tmp/Geodataing/public/data/', TEXT)
        self.assertIn('cp -R public/data/certitude/. /tmp/Geodataing/public/data/certitude/', TEXT)
        self.assertIn('if [ -d public/data/certitude ]; then', TEXT)
        self.assertNotIn('rm -rf /tmp/Geodataing/public/data/certitude', TEXT)

    def test_offline_tests_run_before_collection(self):
        test_step = TEXT.index("run: python -m unittest discover -s tests -p 'test_certitude*.py'")
        collect_step = TEXT.index('run: python crawl_certitude.py')
        self.assertLess(test_step, collect_step)

if __name__ == '__main__':
    unittest.main()
