from copy import deepcopy
import json
import httpx
import pytest
from tests.unit.jenkins.test_llm_workflow import bundle, champion
from jenkins.python.llm_agent_cd.code_evaluation import evaluate, score_payloads, VERSION
from jenkins.python.llm_agent_cd.gates import case_gate
from apps.agentic.llm_ab_router.evaluation_job import ScoreSync


def request(bundle):
    return {'workflow':bundle, 'expected':{'missing_user':True, 'trajectory':[]},
            'duration_seconds':1.5, 'body':{'result':{'task':{
                'status':{'state':'completed'}, 'history':[{'role':'user','parts':[{'text':'Recommend for me'}]}],
                'artifacts':[{'parts':[{'text':'{"clarification":"Please provide user_id."}'}]}]}}}}


def test_missing_user_safe_with_stock_runtime_evidence(bundle):
    result = evaluate(request(bundle))
    assert result['verdict'] == 'PASS'
    scores = {s['name']:s for s in result['scores']}
    assert scores['duplicate_tool_calls']['value'] == 0
    assert scores['ranking_preserved']['status'] == 'NOT_APPLICABLE'
    assert scores['input_tokens']['status'] == 'UNAVAILABLE'
    assert scores['retrieval_recall_at_k']['status'] == 'UNAVAILABLE'


def test_unexpected_tool_call_fails_without_runtime_guard(bundle):
    r = request(bundle)
    r['body']['result']['task']['artifacts'][0]['parts'].append({
        'metadata': {'adk_type':'function_call'},
        'data': {'id':'bad','name':'foreign','args':{}}})
    assert evaluate(r)['verdict'] == 'FAIL'


def test_scores_deterministic_redacted_and_require_real_trace(bundle):
    result = evaluate(request(bundle))
    meta = {'experiment_id':'wf-test','request_key':'request-key','trace_id':'a'*32,
            'source':'synthetic','secret':'must-not-export'}
    wire = score_payloads(result,meta)
    assert wire == score_payloads(result,meta)
    assert len({x['id'] for x in wire}) == len(wire)
    assert 'must-not-export' not in json.dumps(wire)
    with pytest.raises(ValueError): score_payloads(result,{**meta,'trace_id':''})


def test_gate_waits_for_async_scores_without_replaying_cases():
    rows = {str(i):{'verdict':'PASS','release_id':'A' if i<10 else 'B'} for i in range(20)}
    assert case_gate(rows,'A','B',VERSION)[0] == 'HOLD'
    for row in rows.values(): row['evaluation'] = {'verdict':'PASS','evaluator_version':VERSION,'synced':True}
    assert case_gate(rows,'A','B',VERSION)[0] == 'PASS'
    rows['0']['evaluation']['verdict'] = 'FAIL'
    assert case_gate(rows,'A','B',VERSION)[0] == 'FAIL'


@pytest.mark.parametrize('visible,wrong', [(True,False),(False,False),(True,True)])
def test_langfuse_v4_readback_not_http_acceptance(visible,wrong):
    payload = {'id':'score1','traceId':'a'*32,'name':'schema_valid','value':1,'dataType':'BOOLEAN','metadata':{'status':'PASS'}}
    row = {'id':'score1','name':'schema_valid','value':0 if wrong else 1,'dataType':'BOOLEAN',
           'metadata':{'status':'PASS'},'subject':{'kind':'trace','id':'a'*32}}
    calls=[]
    def handle(req):
        calls.append((req.method, req.url.path))
        return httpx.Response(200,json={'id':'score1'} if req.method=='POST' else {'data':[row] if visible else []})
    with httpx.Client(base_url='https://langfuse.test',transport=httpx.MockTransport(handle)) as client:
        if visible and not wrong: ScoreSync(client).confirm(payload)
        else:
            with pytest.raises(ValueError): ScoreSync(client).confirm(payload)
    if visible:
        assert calls == [('GET','/api/public/v3/scores')]
    else:
        assert calls == [('GET','/api/public/v3/scores'),('POST','/api/public/scores')]
