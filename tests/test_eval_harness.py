"""Development-only harness checks. No held-out inference or network request."""
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import os
import unittest
from pydantic_evals import Dataset, Case
from evals.scoring import RoadLensCorrectness, grade_intake
from evals.baseline import _declarations, _tool_specs
from evals.run_suite import summarize

ROOT = Path(os.environ.get('ROADLENS_PROJECT_ROOT', Path(__file__).resolve().parents[1]))


class HarnessTests(unittest.TestCase):
    def test_real_pydantic_evals_api_and_custom_evaluator(self):
        fixture = {'id': 'development-only', 'task': 'intake', 'input': 'Hello', 'expected': {'action': 'out_of_scope'}}
        dataset = Dataset(name='Harness development API test', cases=[Case(name='development-only', inputs={'fixture': fixture}, expected_output={})], evaluators=[RoadLensCorrectness(str(ROOT / 'data'))])
        report = dataset.evaluate_sync(lambda inputs: {'error': None, 'result': {'output': {'kind': 'out_of_scope', 'message': 'This mode accepts road concerns.'}}}, progress=False)
        self.assertEqual(len(report.cases), 1)
        self.assertTrue(report.cases[0].assertions['case_pass'].value)
        self.assertFalse(report.failures)

    def test_remote_module_hydration_never_reads_host_git_or_files(self):
        from unittest.mock import patch
        import importlib.util
        path = Path(__file__).resolve().parents[1] / 'evals/modal_runner.py'
        spec = importlib.util.spec_from_file_location('roadlens_remote_hydration_test', path)
        module = importlib.util.module_from_spec(spec)
        with patch('modal.is_local', return_value=False), patch('subprocess.check_output', side_effect=AssertionError('Remote hydration must not invoke git')), patch('pathlib.Path.read_text', side_effect=AssertionError('Remote hydration must not read local build files')), patch.dict(os.environ, {'ROADLENS_CODE_COMMIT': 'remote-commit', 'ROADLENS_SOURCE_DIRTY': 'false'}):
            spec.loader.exec_module(module)
        self.assertEqual(module.ROOT, Path('/root/roadlens'))
        self.assertEqual(module.COMMIT, 'remote-commit')
        self.assertEqual(module.DIRTY, 'false')

    def test_sdk_tools_have_same_typed_models(self):
        tools, outputs = _tool_specs('evidence')
        declarations = _declarations(tools, outputs)
        names = {d.name for d in declarations}
        self.assertIn('query_metrics', names)
        self.assertIn('get_collision_record', names)
        self.assertIn('final_result_EvidenceAnswer', names)
        self.assertNotIn('execute_sql', names)
        query = next(d for d in declarations if d.name == 'query_metrics')
        self.assertIn('query', query.parameters_json_schema['properties'])

    def test_repeats_are_not_independent_cases(self):
        rows = [{'arm': 'A', 'case_id': 'development-only', 'repeat': repeat, 'task': 'intake', 'passed': repeat == 1,
                 'error': None, 'elapsed_seconds': 1.2, 'checks': {'action': repeat == 1}, 'result': {}} for repeat in (1, 2, 3)]
        summary = summarize(rows)['arms']['A']
        self.assertEqual(summary['unique_cases'], 1)
        self.assertEqual(summary['unique_cases_passed_every_repeat'], 0)
        self.assertEqual(summary['case_attempts_passed'], 1)
        self.assertEqual(summary['case_attempts_total'], 3)

    def test_failure_cannot_look_like_completed(self):
        fixture = {'id': 'development-only', 'input': 'Hello', 'expected': {'action': 'out_of_scope'}}
        checks = grade_intake(fixture, {'error': {'type': 'TimeoutError'}})
        self.assertFalse(checks['completed'])
        self.assertFalse(checks['action'])

if __name__ == '__main__':
    unittest.main()
