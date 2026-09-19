"""Competent plain Google Gen AI SDK loop: same model, prompts, tools and guards.

No Pydantic AI Agent is executed in arm A. Shared Pydantic schemas and deterministic
validators are deliberately retained: security/grounding safeguards are not an AI advantage.
"""
from __future__ import annotations
import asyncio
from datetime import date
import hashlib
import json
import os
import time
from uuid import uuid4
from pydantic import BaseModel, ConfigDict, create_model
from google import genai
from google.genai import types
from backend import agents as shared
from backend.models import (ReadyDraft, NeedsClarification, OutOfScope, EvidenceAnswer,
    InspectionBrief, EvidenceQuery, ResolvedLocation, SourceId, IntakeResponse,
    AnalysisResult, RunSummary)

from .execution import EvaluationExecutionError

MODEL = 'gemini-3.8-flash'


def _plain(value):
    if isinstance(value, BaseModel):
        return value.model_dump(mode='json')
    if isinstance(value, list):
        return [_plain(v) for v in value]
    return value


def _tool_specs(task):
    tools = {'resolve_location': (create_model('ResolveLocation', __config__=ConfigDict(extra='forbid'), text=(str, ...)), shared.resolve_location_impl, 'Resolve actual location wording to supported Cambridge junctions with provenance.')}
    if task != 'intake':
        from typing import Literal
        tools.update({
            'query_metrics': (create_model('QueryMetrics', __config__=ConfigDict(extra='forbid'), query=(EvidenceQuery, ...)), shared.query_metrics_impl, 'Compute collision or casualty counts with source, period, geography, unit and row references.'),
            'find_local_collisions': (create_model('FindLocalCollisions', __config__=ConfigDict(extra='forbid'), location=(ResolvedLocation, ...), start=(date, ...), end=(date, ...), radius_metres=(Literal[50, 100, 250], 100)), shared.find_local_collisions_impl, 'Retrieve separate dated local records around a resolved reference point in projected metres.'),
            'get_source_notes': (create_model('GetSourceNotes', __config__=ConfigDict(extra='forbid'), source_id=(SourceId, ...)), shared.get_source_notes_impl, 'Retrieve official source provenance and limitations.'),
            'get_collision_record': (create_model('GetCollisionRecord', __config__=ConfigDict(extra='forbid'), collision_id=(str, ...)), shared.get_collision_record_impl, 'Retrieve one exact local collision with dated context; collision severity is not casualty severity.'),
        })
    outputs = [ReadyDraft, NeedsClarification, OutOfScope] if task == 'intake' else [EvidenceAnswer, InspectionBrief, NeedsClarification]
    return tools, {f'final_result_{model.__name__}': model for model in outputs}


def _declarations(tools, outputs):
    declarations = [types.FunctionDeclaration(name=name, description=description, parameters_json_schema=model.model_json_schema()) for name, (model, _, description) in tools.items()]
    declarations += [types.FunctionDeclaration(name=name, description=f'Return the final {model.__name__} result, matching the output contract exactly.', parameters_json_schema=model.model_json_schema()) for name, model in outputs.items()]
    return declarations


async def run(task, *, repo, request=None, question=None, api_key=None):
    deps = shared.RoadLensDeps(repo=repo, turns=request.turns if request else [], question=question or '')
    prompt = shared.INTAKE_PROMPT if task == 'intake' else shared.EVIDENCE_PROMPT
    user = shared.intake_user_prompt(request, None) if task == 'intake' else shared.evidence_user_prompt(question, None)
    tool_specs, output_specs = _tool_specs(task)
    raw_calls = []
    usage = {'requests': 0, 'tool_calls': 0, 'input_tokens': 0, 'output_tokens': 0, 'cache_read_tokens': 0, 'details': {}}
    contents = [types.Content(role='user', parts=[types.Part.from_text(text=user)])]
    started = time.perf_counter()
    client = genai.Client(api_key=api_key or os.environ['GEMINI_API_KEY'])
    model_version = 'not_supplied'
    repairs = 0
    try:
        async with asyncio.timeout(90):
            for request_index in range(6):
                remaining = 2500 - usage['output_tokens']
                if remaining <= 0:
                    raise RuntimeError('Output token budget exhausted')
                response = await client.aio.models.generate_content(
                    model=MODEL, contents=contents,
                    config=types.GenerateContentConfig(system_instruction=prompt,
                        tools=[types.Tool(function_declarations=_declarations(tool_specs, output_specs))],
                        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
                        max_output_tokens=remaining))
                usage['requests'] += 1
                meta = response.usage_metadata
                if meta:
                    usage['input_tokens'] += meta.prompt_token_count or 0
                    usage['output_tokens'] += (meta.candidates_token_count or 0) + (getattr(meta, 'thoughts_token_count', 0) or 0)
                    usage['cache_read_tokens'] += getattr(meta, 'cached_content_token_count', 0) or 0
                model_version = getattr(response, 'model_version', None) or model_version
                candidates = response.candidates or []
                if not candidates or candidates[0].content is None:
                    raise RuntimeError('Provider returned no candidate')
                content = candidates[0].content
                # Keep full structured parts only in memory to preserve provider thought signatures.
                # Persist authored-fixture visible answers/tool calls, never reasoning/signatures.
                raw_calls.append({'request_index': request_index, 'model_version': model_version,
                    'parts': [{'function_call': p.function_call.model_dump(mode='json', exclude_none=True)} if p.function_call else {'text': p.text}
                              for p in content.parts or [] if not getattr(p, 'thought', False) and (p.function_call or p.text)]})
                contents.append(content)
                calls = [p.function_call for p in content.parts or [] if p.function_call]
                if not calls:
                    repairs += 1
                    if repairs > 2:
                        raise RuntimeError('Output repair budget exhausted')
                    deps.repairs.append('Return a structured final-result tool call, not unstructured prose.')
                    contents.append(types.Content(role='user', parts=[types.Part.from_text(text=deps.repairs[-1])]))
                    continue
                responses = []
                completed = None
                for call in calls:
                    try:
                        args = dict(call.args or {})
                        if call.name in output_specs:
                            if len(calls) != 1:
                                raise ValueError('Return exactly one final output after tool results have been received.')
                            output = output_specs[call.name].model_validate(args)
                            validator = shared.validate_intake_output if task == 'intake' else shared.validate_evidence_output
                            completed = validator(deps, output)
                            value = {'accepted': True}
                        elif call.name in tool_specs:
                            if usage['tool_calls'] >= 8:
                                raise RuntimeError('Tool-call budget exhausted')
                            usage['tool_calls'] += 1
                            schema, function, _ = tool_specs[call.name]
                            parsed = schema.model_validate(args)
                            value = _plain(function(deps, **{name: getattr(parsed, name) for name in schema.model_fields}))
                        else:
                            raise ValueError('Unknown tool. Use only declared tools.')
                    except Exception as exc:
                        if isinstance(exc, RuntimeError):
                            raise
                        repairs += 1
                        if repairs > 2:
                            raise RuntimeError('Validation repair budget exhausted') from None
                        # Tool/domain validation messages are authored by the shared validators;
                        # Pydantic input errors are reduced to avoid reflecting secret-shaped text.
                        error = str(exc) if type(exc).__name__ == 'ModelRetry' else 'Invalid arguments; match the declared schema and supported values.'
                        if not deps.repairs or deps.repairs[-1] != error:
                            deps.repairs.append(error)
                        value = {'validation_error': error, 'repair_required': True}
                    responses.append(types.Part(function_response=types.FunctionResponse(name=call.name, id=getattr(call, 'id', None), response={'result': value})))
                if usage['output_tokens'] > 2500:
                    raise RuntimeError('Output token budget exceeded')
                if completed is not None:
                    summary = RunSummary(run_id='run:' + uuid4().hex, model=MODEL, model_version=model_version,
                        tools=deps.tools, validation_repairs=deps.repairs, usage=usage,
                        elapsed_seconds=time.perf_counter()-started, outcome=completed.kind,
                        data_hashes=repo.hashes, prompt_hash=hashlib.sha256(prompt.encode()).hexdigest(),
                        code_commit=os.getenv('ROADLENS_CODE_COMMIT', 'working-tree-uncommitted'))
                    if task == 'intake':
                        result = IntakeResponse(output=completed, run=summary)
                    else:
                        result = AnalysisResult(output=completed, evidence=shared.evidence_bundle(deps, completed), run=summary)
                    return result.model_dump(mode='json'), raw_calls
                contents.append(types.Content(role='user', parts=responses))
            raise RuntimeError('Model request budget exhausted')
    except Exception as exc:
        partial = {'run_id': 'run:' + uuid4().hex, 'model': MODEL, 'model_version': model_version,
            'tools': [_plain(event) for event in deps.tools], 'validation_repairs': deps.repairs, 'usage': usage,
            'elapsed_seconds': time.perf_counter()-started, 'outcome': 'failed', 'data_hashes': repo.hashes, 'usage_partial': True,
            'prompt_hash': hashlib.sha256(prompt.encode()).hexdigest(), 'code_commit': os.getenv('ROADLENS_CODE_COMMIT', 'working-tree-uncommitted')}
        raise EvaluationExecutionError(exc, partial, raw_calls) from None
    finally:
        await client.aio.aclose()
        client.close()
