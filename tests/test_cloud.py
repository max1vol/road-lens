import asyncio
from datetime import datetime, timezone
import json
import pytest
import httpx
from pydantic_ai import ModelRetry
from backend.agents import *
from backend.models import *
from backend.service import create_api
from backend.worker import ClaimedJob, process_job, LeaseLost

@pytest.fixture
def repo(): return Repository()

def turn(text, n=1):
    return ResidentTurn(turn_id=f't{n}', text=text, timestamp=datetime.now(timezone.utc))

def draft(deps, text='I cannot see past parked vehicles when crossing.'):
    deps.turns = [turn(text), turn('Vicarage Terrace at St Matthews Street.',2)]
    loc = resolve_location_impl(deps, deps.turns[1].text)[0]
    return ReadyDraft(issue='visibility_obstruction', observation=text, location=loc,
                      evidence_spans=[EvidenceSpan(turn_id='t1', quote=text)])

def test_readback_grounded_and_deterministic(repo):
    deps = RoadLensDeps(repo)
    d = draft(deps)
    out = validate_intake_output(deps,d)
    assert out.readback == readback_for(d)
    assert 'Vicarage Terrace and St Matthews Street' in out.readback
    assert deps.tools[0].inputs.get('text') is None

def test_reject_fabricated_location(repo):
    deps = RoadLensDeps(repo)
    d = draft(deps)
    d.location = d.location.model_copy(update={'latitude':52.99})
    with pytest.raises(ModelRetry): validate_intake_output(deps,d)

def test_reject_contact_and_negation(repo):
    for text in ['There is no pothole here.', 'My email is contact@example.invalid and the road is blocked.']:
        deps = RoadLensDeps(repo)
        d = draft(deps,text)
        with pytest.raises(ModelRetry): validate_intake_output(deps,d)

def test_reject_new_location_not_in_actual_turn(repo):
    deps = RoadLensDeps(repo, turns=[turn('New Street')])
    assert resolve_location_impl(deps,'New Street') == []
    with pytest.raises(ModelRetry): resolve_location_impl(deps,'York Street at New Street')

def test_corrected_location_supersedes(repo):
    deps = RoadLensDeps(repo)
    d = draft(deps)
    deps.turns.append(turn('Actually, Sturton Street at New Street, not Vicarage Terrace.',3))
    with pytest.raises(ModelRetry): validate_intake_output(deps,d)

def test_evidence_registry_enforces_refs_and_limitations(repo):
    deps = RoadLensDeps(repo, question='Did parked cars cause the collision?')
    with pytest.raises(ModelRetry): validate_evidence_output(deps,EvidenceAnswer(metric_refs=['metric:invented']))
    with pytest.raises(ModelRetry): validate_evidence_output(deps,EvidenceAnswer())
    result = query_metrics_impl(deps,EvidenceQuery(road_user='cyclist'))
    assert result.value == 121
    out = validate_evidence_output(deps,EvidenceAnswer(metric_refs=[result.reference_id], limitations=['no_causal_evidence']))
    assert evidence_bundle(deps,out).metrics[0].query_hash == result.query_hash

def test_local_record_severity_is_not_casualty_severity(repo):
    deps = RoadLensDeps(repo)
    record = get_collision_record_impl(deps,'1737997')
    assert record.casualty_count == 2
    assert record.severity == 'Serious'
    assert record.provisional

@pytest.mark.asyncio
async def test_auth_before_parse_and_payload_bound(monkeypatch):
    monkeypatch.setenv('ROADLENS_MODAL_SERVICE_TOKEN','unit-test-token')
    calls = []
    async def classifier(text): calls.append('classifier'); raise AssertionError('should not run')
    async def wake(): calls.append('wake'); return 'fc:test'
    app = create_api(classifier,wake)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='https://service.test') as client:
        denied = await client.post('/prepare', content='not-json')
        assert denied.status_code == 401
        big = await client.post('/prepare', headers={'Authorization':'Bearer unit-test-token'},content='x'*16385)
        assert big.status_code == 413
        ok = await client.post('/wake',headers={'Authorization':'Bearer unit-test-token'})
        assert ok.status_code == 202 and ok.json()['accepted']
    assert calls == ['wake']

def analysis_result():
    return AnalysisResult(output=EvidenceAnswer(), evidence=EvidenceBundle(sources=[]), run=RunSummary(
        run_id='run:test',model=MODEL,model_version='test',usage={},elapsed_seconds=.1,outcome='evidence_answer',
        data_hashes={},prompt_hash='test',code_commit='test'))

@pytest.mark.asyncio
async def test_worker_lease_loss_cancels_without_result():
    callbacks = []
    async def handler(req):
        callbacks.append(req.url.path)
        return httpx.Response(409,json={'error':'lease_lost'})
    job = ClaimedJob(id='job:test',kind='officer_question',payload={'question':'How many people?'},lease_token='lease:test',attempts=1)
    async def slow(question,report):
        await asyncio.sleep(1)
        return analysis_result()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler),base_url='https://site.test') as client:
        with pytest.raises(LeaseLost): await process_job(client,job,analyze_fn=slow,renewal_interval=.005)
    assert callbacks == ['/api/internal/jobs/job:test/renew']

@pytest.mark.asyncio
async def test_worker_post_typed_result_once():
    requests=[]
    async def handler(req):
        requests.append(req)
        return httpx.Response(200,json={'ok':True})
    job=ClaimedJob(id='job:test',kind='officer_question',payload={'question':'How many people?'},lease_token='lease:test',attempts=1)
    async def fast(question,report): return analysis_result()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler),base_url='https://site.test') as client:
        assert await process_job(client,job,analyze_fn=fast) == 'complete'
    assert len(requests)==1
    payload=JobResult.model_validate_json(requests[0].content)
    assert payload.lease_token=='lease:test' and payload.result.run.run_id=='run:test'

@pytest.mark.asyncio
async def test_real_agent_tool_dispatch_and_run_summary(repo):
    from pydantic_ai.models.function import FunctionModel
    from pydantic_ai.messages import ModelResponse,ToolCallPart
    calls=0
    location=repo.location('vicarage-st-matthews')
    observation='I cannot see around parked vehicles.'
    async def model(messages,info):
        nonlocal calls
        calls+=1
        if calls==1:
            return ModelResponse(parts=[ToolCallPart('resolve_location',{'text':'Vicarage Terrace at St Matthews Street'})])
        output=ReadyDraft(issue='visibility_obstruction',observation=observation,location=location,
                          evidence_spans=[EvidenceSpan(turn_id='t1',quote=observation)])
        name=next(t.name for t in info.output_tools if 'ReadyDraft' in t.name)
        return ModelResponse(parts=[ToolCallPart(name,output.model_dump(mode='json'))],model_name='unit-test-model')
    result=await prepare(PrepareRequest(session_id='s1',turns=[turn(observation),turn('Vicarage Terrace at St Matthews Street',2)]),repo=repo,model=FunctionModel(model))
    assert result.output.kind=='ready_for_confirmation'
    assert result.run.usage['requests']==2
    assert result.run.tools[0].name=='resolve_location'
    assert result.run.model_version.startswith('function:')

@pytest.mark.asyncio
async def test_real_evidence_agent_tool_dispatch(repo):
    from pydantic_ai.models.function import FunctionModel
    from pydantic_ai.messages import ModelResponse,ToolCallPart
    calls=0
    query=EvidenceQuery(road_user='cyclist')
    async def model(messages,info):
        nonlocal calls
        calls+=1
        if calls==1: return ModelResponse(parts=[ToolCallPart('query_metrics',{'query':query.model_dump(mode='json')})])
        name=next(t.name for t in info.output_tools if 'EvidenceAnswer' in t.name)
        return ModelResponse(parts=[ToolCallPart(name,{'metric_refs':[repo.query(query).reference_id]})])
    result=await analyze('Cyclist casualties in Cambridge in 2025',repo=repo,model=FunctionModel(model))
    assert result.evidence.metrics[0].value==121
    assert result.run.tools[0].name=='query_metrics'

def test_valid_resident_observation_does_not_require_keyword_allowlist(repo):
    # New authored development wording. Semantic classification belongs to the
    # reasoning agent; deterministic validation checks provenance and scope rules.
    deps=RoadLensDeps(repo)
    d=draft(deps,'A paving slab is loose here.').model_copy(update={'issue':Issue.road_surface})
    assert validate_intake_output(deps,d).kind=='ready_for_confirmation'
