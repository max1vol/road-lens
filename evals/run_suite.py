"""Run 35 authored model cases × A/B/C; five workflow cases run separately.

No scored case is used for prompt tuning here. Repetitions remain grouped by fixture.
Use --validate-only to check frozen inputs without making model/classifier calls.
"""
from __future__ import annotations
import argparse
import asyncio
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import statistics
import subprocess
import time
from uuid import uuid4
from pydantic_evals import Case, Dataset
from pydantic_ai.usage import UsageLimits, RunUsage
from pydantic_ai import capture_run_messages
from .execution import EvaluationExecutionError, visible_messages, json_default, json_safe
from backend.models import ClassifierSuggestion, IntakeResponse, AnalysisResult
from backend.evidence import digest
from backend import agents
from backend.evidence import Repository
from backend.models import PrepareRequest, ResidentTurn
from .baseline import run as run_baseline
from .scoring import RoadLensCorrectness, grade


def hash_file(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def freeze_metadata(root):
    freeze = json.loads((root / 'research/frozen-inputs.json').read_text())
    mismatches = [relative for relative, sha in freeze['sha256'].items() if not (root / relative).is_file() or hash_file(root / relative) != sha]
    if mismatches:
        raise RuntimeError('Frozen fixtures/data changed: ' + ', '.join(mismatches))
    try:
        commit = subprocess.check_output(['git', '-C', str(root), 'rev-parse', 'HEAD'], stderr=subprocess.DEVNULL, text=True).strip()
        dirty = bool(subprocess.check_output(['git', '-C', str(root), 'status', '--porcelain'], text=True).strip())
    except (OSError, subprocess.CalledProcessError):
        commit = os.getenv('ROADLENS_CODE_COMMIT', 'uncommitted')
        dirty = os.getenv('ROADLENS_SOURCE_DIRTY', 'true') == 'true'
    code_hashes = {str(path.relative_to(root)): hash_file(path) for folder in ('backend', 'evals', 'box_adapter')
                   for path in sorted((root / folder).rglob('*.py')) if '__pycache__' not in path.parts}
    return {'fixture_freeze': freeze, 'code_commit': commit, 'working_tree_dirty': dirty,
            'code_sha256': code_hashes, 'model': agents.MODEL,
            'prompt_sha256': {name: hashlib.sha256(value.encode()).hexdigest() for name, value in [('intake', agents.INTAKE_PROMPT), ('evidence', agents.EVIDENCE_PROMPT)]},
            'versions': {package: importlib.metadata.version(package) for package in ('pydantic-ai-slim', 'pydantic-evals', 'google-genai', 'pydantic', 'modal')},
            'budgets': {'requests': 6, 'tool_calls': 8, 'output_tokens': agents.AGENT_OUTPUT_TOKEN_LIMIT, 'output_tokens_per_response': agents.AGENT_MODEL_SETTINGS['max_tokens'], 'thinking': agents.AGENT_MODEL_SETTINGS['google_thinking_config'], 'wall_clock_seconds': 90},
            'max_concurrency': 3,
            'budget_revision': 'Before any scored inference: development smoke showed default MEDIUM thinking exceeded the initial 2500 aggregate output limit (7547 observed tokens). All arms now share LOW thinking, 4096 maximum per response and 12000 aggregate output tokens. No scored fixtures informed this change.',
            'scope': '35 authored model fixtures (20 intake, 12 evidence, 3 grounded-behaviour) plus five separate workflow scenarios; not independent human-reviewed held-out evidence.',
            'comparison': 'A plain Google SDK; B Pydantic AI; C Pydantic AI with actual pinned Modal GLiNER intake preprocessing. Evidence B and C intentionally share one execution path. Common domain/write/security guards remain enabled in every arm.',
            'gold_review': freeze.get('gold_review', 'Independent human review not recorded')}


def load_fixtures(root):
    intake = [dict(case, task='intake') for case in json.loads((root / 'evals/intake_cases.json').read_text())]
    evidence = [dict(case, task='evidence') for case in json.loads((root / 'evals/evidence_cases.json').read_text())]
    behaviour = [dict(case, task='behaviour', input=case['scenario']) for case in json.loads((root / 'evals/behaviour_cases.json').read_text()) if case['id'] in ('B05', 'B06', 'B07')]
    assert (len(intake), len(evidence), len(behaviour)) == (20, 12, 3)
    return intake + evidence + behaviour


def intake_request(fixture):
    # This timestamp is explicitly synthetic evaluation metadata, not a resident event.
    return PrepareRequest(session_id='eval:' + fixture['id'], turns=[ResidentTurn(turn_id='eval:' + fixture['id'] + ':turn1', text=fixture['input'], timestamp=datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc))])


async def typed_run(fixture, *, repo, classifier=None):
    """Production builders/helpers, with visible raw response capture for authored cases."""
    request = intake_request(fixture) if fixture['task'] == 'intake' else None
    deps = agents.RoadLensDeps(repo=repo, turns=request.turns if request else [], question='' if request else fixture['input'])
    started = time.perf_counter()
    prompt = agents.INTAKE_PROMPT if request else agents.EVIDENCE_PROMPT
    messages = []
    try:
        with capture_run_messages() as messages:
            async with asyncio.timeout(90):
                if request and classifier:
                    text = '\n'.join(turn.text for turn in request.turns)
                    deps.classifier = ClassifierSuggestion.model_validate(await classifier(text))
                    deps.event('classify_resident_text', {'input_sha256': digest(text), 'characters': len(text)}, agents.redacted_classifier(deps.classifier).model_dump(mode='json'))
                model = agents.google_model()
                agent = agents.create_intake_agent(model) if request else agents.create_evidence_agent(model)
                user = agents.intake_user_prompt(request, deps.classifier) if request else agents.evidence_user_prompt(fixture['input'], None)
                result = await agent.run(user, deps=deps, usage_limits=UsageLimits(request_limit=6, tool_calls_limit=8, output_tokens_limit=agents.AGENT_OUTPUT_TOKEN_LIMIT))
    except Exception as exc:
        usage = RunUsage()
        for message in messages:
            if getattr(message, 'kind', '') == 'response':
                usage.incr(message.usage)
                usage.requests += 1
                usage.tool_calls += sum(getattr(part, 'part_kind', '') == 'tool-call' and not getattr(part, 'tool_name', '').startswith('final_result') for part in message.parts)
        partial = {'run_id': 'run:' + uuid4().hex, 'model': agents.MODEL, 'model_version': 'not_supplied',
            'tools': [event.model_dump(mode='json') for event in deps.tools], 'validation_repairs': deps.repairs, 'usage': asdict(usage),
            'elapsed_seconds': time.perf_counter()-started, 'outcome': 'failed', 'data_hashes': repo.hashes, 'usage_partial': True,
            'classifier': agents.redacted_classifier(deps.classifier).model_dump(mode='json') if deps.classifier else None,
            'prompt_hash': hashlib.sha256(prompt.encode()).hexdigest(), 'code_commit': os.getenv('ROADLENS_CODE_COMMIT', 'working-tree-uncommitted')}
        raise EvaluationExecutionError(exc, partial, visible_messages(messages)) from None
    summary = agents.run_summary(result, deps, prompt, time.perf_counter()-started)
    wrapped = IntakeResponse(output=result.output, run=summary) if request else AnalysisResult(output=result.output, evidence=agents.evidence_bundle(deps, result.output), run=summary)
    return wrapped.model_dump(mode='json'), visible_messages(result.all_messages())


def summarize(rows):
    result = {'arms': {}, 'comparative_claim': 'No workflow/security guarantees are counted as AI-accuracy gains.'}
    for arm in sorted({row['arm'] for row in rows}):
        arm_rows = [row for row in rows if row['arm'] == arm]
        groups = {}
        for row in arm_rows:
            groups.setdefault(row['case_id'], []).append(row)
        succeeded = [row for row in arm_rows if row['error'] is None]
        result['arms'][arm] = {
            'case_attempts_passed': sum(row['passed'] for row in arm_rows), 'case_attempts_total': len(arm_rows),
            'unique_cases': len(groups), 'unique_cases_passed_every_repeat': sum(all(r['passed'] for r in group) for group in groups.values()),
            'completion_numerator': len(succeeded), 'completion_denominator': len(arm_rows),
            'median_latency_seconds_all_attempts': statistics.median(r['elapsed_seconds'] for r in arm_rows),
            'validation_repairs': sum(len(r.get('result', {}).get('run', {}).get('validation_repairs', [])) for r in arm_rows),
            'usage': {key: sum(r.get('result', {}).get('run', {}).get('usage', {}).get(key, 0) or 0 for r in arm_rows) for key in ('requests', 'tool_calls', 'input_tokens', 'output_tokens')},
            'failed_cases': [{'case_id': r['case_id'], 'repeat': r['repeat'], 'checks': [k for k, v in r['checks'].items() if not v], 'error': r['error']} for r in arm_rows if not r['passed']],
            'by_task': {task: {'passed': sum(r['passed'] for r in arm_rows if r['task'] == task), 'total': sum(r['task'] == task for r in arm_rows)} for task in ('intake', 'evidence', 'behaviour')},
        }
    return result


async def execute(args):
    root = Path(args.project_root).resolve()
    metadata = freeze_metadata(root)
    if agents.MODEL != 'gemini-3.8-flash':
        raise RuntimeError('Exact model required; silent substitutions are forbidden')
    fixtures = load_fixtures(root)
    if args.validate_only:
        print(json.dumps({'frozen_inputs': 'verified', 'model_case_count': len(fixtures), 'workflow_case_count': 5, 'human_review': metadata['gold_review']}))
        return
    if not args.execute:
        raise RuntimeError('Use --execute for paid provider/classifier calls, or --validate-only')
    if not os.getenv('GEMINI_API_KEY'):
        raise RuntimeError('GEMINI_API_KEY is missing; provide it through a private environment, never a command argument')
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    output.chmod(0o700)
    metadata.update(run_id='evaluation:' + uuid4().hex, started_at=datetime.now(timezone.utc).isoformat(), repetitions=args.repetitions, arms=args.arms)
    os.environ['ROADLENS_CODE_COMMIT'] = metadata['code_commit']
    (output / 'manifest.json').write_text(json.dumps(metadata, indent=2, default=json_default, allow_nan=False))
    repo = Repository(root / 'data')
    classifier = None
    if 'C' in args.arms:
        import modal
        classifier_instance = modal.Cls.from_name(args.modal_app, 'Classifier')()
        async def classifier(text):
            return await classifier_instance.predict.remote.aio(text)
    cases = []
    for repeat in range(1, args.repetitions + 1):
        for index, fixture in enumerate(fixtures):
            offset = (index + repeat - 1) % len(args.arms)
            arm_order = args.arms[offset:] + args.arms[:offset]
            for arm in arm_order:
                cases.append(Case(name=f'{fixture["id"]}:{arm}:r{repeat}', inputs={'fixture': fixture, 'arm': arm, 'repeat': repeat}, expected_output=fixture.get('expected', fixture)))
    rows = []

    async def task(inputs):
        fixture, arm, repeat = inputs['fixture'], inputs['arm'], inputs['repeat']
        start = time.perf_counter()
        envelope = {'case_id': fixture['id'], 'task': fixture['task'], 'arm': arm, 'repeat': repeat,
                    'input': fixture['input'], 'expected': fixture.get('expected', fixture), 'error': None, 'raw_model_calls': []}
        try:
            if arm == 'A':
                result, raw = await run_baseline(fixture['task'], repo=repo,
                    request=intake_request(fixture) if fixture['task'] == 'intake' else None,
                    question=fixture['input'] if fixture['task'] != 'intake' else None)
                envelope.update(result=result, raw_model_calls=raw)
            else:
                result, raw = await typed_run(fixture, repo=repo, classifier=classifier if arm == 'C' else None)
                envelope.update(result=result, raw_model_calls=raw)
        except Exception as exc:
            # Never export HTTP headers, raw exception messages or credential-bearing URLs.
            envelope['error'] = {'type': getattr(exc, 'cause_type', type(exc).__name__), 'stage': 'model_or_classifier_execution', 'details': 'See protected runtime diagnostics; no secret-bearing exception text exported.'}
            if isinstance(exc, EvaluationExecutionError):
                envelope['result'] = {'run': exc.run}
                envelope['raw_model_calls'] = exc.raw
        envelope['elapsed_seconds'] = round(time.perf_counter() - start, 6)
        envelope['checks'] = grade(fixture, envelope, repo)
        envelope['passed'] = all(envelope['checks'].values())
        envelope['provenance'] = {'prompt_sha256': metadata['prompt_sha256']['intake' if fixture['task'] == 'intake' else 'evidence'], 'code_commit': metadata['code_commit'], 'model': metadata['model'], 'data_sha256': repo.hashes}
        # Fixtures are authored test data. This exporter must never process live resident turns.
        envelope = json_safe(envelope)
        serialized = json.dumps(envelope, ensure_ascii=False, allow_nan=False)
        if os.environ['GEMINI_API_KEY'] in serialized:
            raise RuntimeError('Secret exposure detected: refusing evaluation export')
        with (output / 'cases.jsonl').open('a') as stream:
            stream.write(serialized + '\n')
        rows.append(envelope)
        (output / 'summary.json').write_text(json.dumps(summarize(rows), indent=2, default=json_default, allow_nan=False))
        print(f'{fixture["id"]} arm {arm} repeat {repeat}: {"pass" if envelope["passed"] else "FAIL"}, {envelope["elapsed_seconds"]:.2f}s', flush=True)
        return envelope

    dataset = Dataset(name='RoadLens fixed authored model comparison', cases=cases,
                      evaluators=[RoadLensCorrectness(data_root=str(root / 'data'))])
    report = await dataset.evaluate(task, name=metadata['run_id'], max_concurrency=3, progress=False)
    # Custom evaluator results are saved directly, alongside all task outputs in cases.jsonl.
    report_rows = [{'case_name': case.name, 'assertions': {name: {'value': value.value, 'reason': value.reason} for name, value in case.assertions.items()}, 'scores': {name: value.value for name, value in case.scores.items()}} for case in report.cases]
    (output / 'pydantic-evals-report.json').write_text(json.dumps({'name': report.name, 'cases': report_rows, 'evaluator_failures': len(report.failures)}, indent=2, default=json_default, allow_nan=False))
    metadata['finished_at'] = datetime.now(timezone.utc).isoformat()
    (output / 'manifest.json').write_text(json.dumps(metadata, indent=2, default=json_default, allow_nan=False))
    print(f'Results saved to {output}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project-root', default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument('--output', default='research/evaluation-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ'))
    parser.add_argument('--arms', nargs='+', choices=['A', 'B', 'C'], default=['A', 'B', 'C'])
    parser.add_argument('--repetitions', type=int, choices=[1, 3], default=1)
    parser.add_argument('--modal-app', default='roadlens-cambridge')
    parser.add_argument('--validate-only', action='store_true')
    parser.add_argument('--execute', action='store_true')
    asyncio.run(execute(parser.parse_args()))

if __name__ == '__main__':
    main()
