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

    def test_decimal_failure_export_and_exception_round_trip(self):
        from decimal import Decimal
        import pickle
        from evals.execution import EvaluationExecutionError, json_safe
        failure = EvaluationExecutionError(RuntimeError('provider text is deliberately omitted'),
            {'usage': {'cost': Decimal('0.000001234567890123456789')}}, [{'parts': [{'text': 'visible output'}]}])
        restored = pickle.loads(pickle.dumps(failure))
        self.assertEqual(restored.cause_type, 'RuntimeError')
        result = restored.as_result()
        self.assertFalse(result['ok'])
        self.assertEqual(result['run']['usage']['cost'], '0.000001234567890123456789')
        self.assertNotIn('provider text', json.dumps(result))
        with self.assertRaises(TypeError):
            json_safe(object())

    def test_plain_sdk_uses_shared_production_settings(self):
        from unittest.mock import patch, AsyncMock
        from types import SimpleNamespace
        from google.genai import types
        from backend import agents
        from backend.evidence import Repository
        from backend.models import PrepareRequest, ResidentTurn
        from evals.baseline import run
        response = types.GenerateContentResponse(model_version='development-test',
            usage_metadata=types.GenerateContentResponseUsageMetadata(prompt_token_count=10, candidates_token_count=5, thoughts_token_count=2),
            candidates=[types.Candidate(content=types.Content(role='model', parts=[types.Part(function_call=types.FunctionCall(
                name='final_result_OutOfScope', args={'kind': 'out_of_scope', 'message': 'RoadLens accepts road concerns.'}))]))])
        generate = AsyncMock(return_value=response)
        client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=generate), aclose=AsyncMock()), close=lambda: None)
        request = PrepareRequest(session_id='development-config-check', turns=[ResidentTurn(turn_id='development-turn', text='My favourite colour is blue.', timestamp=datetime.now(timezone.utc))])
        with patch('evals.baseline.genai.Client', return_value=client):
            result, raw = asyncio.run(run('intake', repo=Repository(ROOT / 'data'), request=request, api_key='development-not-a-real-key'))
        config = generate.call_args.kwargs['config']
        self.assertEqual(config.max_output_tokens, agents.AGENT_MODEL_SETTINGS['max_tokens'])
        self.assertEqual(config.thinking_config.thinking_level.value, agents.AGENT_MODEL_SETTINGS['google_thinking_config']['thinking_level'])
        self.assertFalse(config.thinking_config.include_thoughts)
        self.assertEqual(agents.AGENT_OUTPUT_TOKEN_LIMIT, 12000)
        self.assertEqual(result['run']['usage']['output_tokens'], 7)
        json.dumps(raw)

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
