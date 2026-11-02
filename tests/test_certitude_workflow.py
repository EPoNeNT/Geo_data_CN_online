import re
import unittest
from pathlib import Path

try:
    import yaml
except ImportError:
    yaml = None

WORKFLOW = Path('.github/workflows/crawl.yml')
TEXT = WORKFLOW.read_text(encoding='utf-8')


def job_block(name):
    match = re.search(rf'^  {re.escape(name)}:\n(.*?)(?=^  [a-z0-9-]+:\n|\Z)', TEXT, re.M | re.S)
    if not match:
        raise AssertionError(f'missing job: {name}')
    return match.group(1)

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
        self.assertIn('publication_page_limit', TEXT)

    def test_usage_audit_is_manual_independent_and_never_generates_or_pushes(self):
        audit = job_block('audit-certitude-usage')
        self.assertIn('          - certitude-audit\n', TEXT)
        self.assertIn("github.event_name == 'workflow_dispatch'", audit)
        self.assertIn("github.event.inputs.mode == 'certitude-audit'", audit)
        self.assertNotIn("github.event_name == 'schedule'", audit)
        self.assertNotIn('needs:', audit)
        self.assertIn('python audit_certitude_usage.py "${args[@]}"', audit)
        self.assertIn('args+=(--apply-exclusions)', audit)
        self.assertIn('timeout-minutes: 360', audit)
        self.assertIn('Restore encrypted nonpremium cookie state', audit)
        self.assertIn('Save encrypted nonpremium cookie state', audit)
        self.assertIn('include-hidden-files: true', audit)
        self.assertIn('.runtime/certitude-usage-audit/*.json', audit)
        self.assertNotIn('python generate_certitude.py', audit)
        self.assertNotIn('git push', audit)
        self.assertNotIn('git commit', audit)
        apply_input = TEXT.split('      audit_apply_exclusions:', 1)[1].split('concurrency:', 1)[0]
        self.assertIn('default: true', apply_input)

    def test_job_runs_certitude_and_preserves_static_deploy_paths(self):
        self.assertIn("run: python -m unittest discover -s tests -p 'test_certitude*.py'", TEXT)
        self.assertIn('python crawl_certitude.py --publication-page-limit "$CERTITUDE_PUBLICATION_PAGE_LIMIT"', TEXT)
        self.assertIn('crawl-certitude:', TEXT)
        self.assertIn('python generate_certitude.py', TEXT)
        self.assertIn('cp public/data/*.json /tmp/Geodataing/public/data/', TEXT)
        self.assertIn('cp -R public/data/certitude/. /tmp/Geodataing/public/data/certitude/', TEXT)
        self.assertIn('if [ -d public/data/certitude ]; then', TEXT)
        self.assertNotIn('rm -rf /tmp/Geodataing/public/data/certitude', TEXT)


    def test_collection_is_separate_and_generation_waits_for_it(self):
        collector, generator = job_block('crawl-certitude'), job_block('generate-data')
        self.assertIn('needs: crawl-logs', collector)
        self.assertIn("github.event.inputs.mode == 'certitude'", collector)
        self.assertIn("github.event.inputs.mode == 'both'", collector)
        self.assertIn("github.event_name == 'schedule'", collector)
        collector_if = next(line for line in collector.splitlines() if line.strip().startswith('if:'))
        self.assertNotIn("github.event.inputs.mode == 'generate'", collector_if)
        self.assertNotIn('python generate_certitude.py', collector)
        self.assertIn('needs: [crawl-logs, crawl-certitude]', generator)
        self.assertNotIn("github.event.inputs.mode == 'certitude'", generator.split('steps:', 1)[0])
        gen_step = generator[generator.index('- name: Generate Certitude static data'):]
        gen_if = gen_step.split('run: python generate_certitude.py', 1)[0]
        self.assertIn('if: success() &&', gen_if)
        self.assertIn("needs.crawl-certitude.result != 'failure'", gen_if)
        self.assertIn("needs.crawl-certitude.result != 'cancelled'", gen_if)
        self.assertNotIn('python crawl_certitude.py', generator)

    def test_cookie_state_settings_and_collection_command_are_only_in_crawler(self):
        collector, generator = job_block('crawl-certitude'), job_block('generate-data')
        self.assertNotIn('GEOCACHING_COOKIE_STATE_KEY', collector)
        self.assertIn('CERTITUDE_PUBLICATION_PAGE_LIMIT', collector)
        self.assertNotIn('Restore encrypted nonpremium cookie state', collector)
        self.assertNotIn('Save encrypted nonpremium cookie state', collector)
        self.assertNotIn('Restore encrypted nonpremium cookie state', generator)
        self.assertNotIn('Save encrypted nonpremium cookie state', generator)
        self.assertNotIn('CERTITUDE_PUBLICATION_PAGE_LIMIT', generator)
        self.assertNotIn('python crawl_certitude.py', generator)

    def test_offline_tests_run_before_collection(self):
        job = TEXT.split('  crawl-certitude:', 1)[1].split('  generate-data:', 1)[0]
        test_step = job.index("run: python -m unittest discover -s tests -p 'test_certitude*.py'")
        collect_step = job.index('run: python crawl_certitude.py')
        self.assertLess(test_step, collect_step)

if __name__ == '__main__':
    unittest.main()
