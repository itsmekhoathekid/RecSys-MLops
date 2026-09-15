"""Exactly-six and crash ambiguity checks against disposable PostgreSQL."""
import os
import uuid
import json
import pytest
from apps.agentic.llm_ab_router.database import Database
from jenkins.python.llm_agent_cd.offline import run, observation, VERSION
from jenkins.python.llm_agent_cd.release import digest
from jenkins.python.llm_agent_cd.small_compatibility import smoke_fixtures
from jenkins.python.llm_agent_cd.workflow_contract import SAFETY
from tests.unit.jenkins.test_llm_workflow import bundle, champion, candidate

DSN=os.environ.get('LLM_AB_TEST_DATABASE_URL','')
pytestmark=pytest.mark.skipif(not DSN,reason='disposable localhost PostgreSQL required')


@pytest.fixture
def state_db(bundle):
    assert '127.0.0.1' in DSN
    db=Database(DSN);db.migrate()
    fixtures=smoke_fixtures(SAFETY)
    return db,{'experiment_id':'wf-'+uuid.uuid4().hex,'baseline':bundle,
        'pending':candidate(bundle,llm=True),'compatibility_fixtures':fixtures,
        'compatibility_fixture_checksum':digest(fixtures)}


def responder(counter):
    def call(manifest,variant,body):
        counter.append((variant,body))
        text=body['messages'][-1]['content']
        if 'get_context' in text: name,args='get_context',{'item_ids':[7,4]}
        elif '1002' in text: name,args='get_recommendations',{'user_id':1002,'top_k':2}
        else: name,args='get_recommendations',{'user_id':1001,'top_k':3}
        return {'choices':[{'message':{'tool_calls':[{'function':{'name':name,'arguments':json.dumps(args)}}]}}]}
    return call


def test_exact_six_never_repeated_by_job_retry_and_sync_pending_holds(state_db):
    db,state=state_db; calls=[]
    run(db,state,responder(calls)); run(db,state,responder(calls))
    assert len(calls)==6 and [v for v,_ in calls]==['control']*3+['candidate']*3
    assert observation(db,state)['verdict']=='HOLD'
    with db.connect() as c:
        c.execute('UPDATE recsys_ab.evaluation_outbox SET confirmed_at=now() WHERE experiment_id=%s',(state['experiment_id'],))
    assert observation(db,state)['verdict']=='PASS'


def test_ambiguous_sent_request_is_not_replayed(state_db):
    db,state=state_db
    key=digest([state['experiment_id'],VERSION,'control','tool_selection'])
    with db.connect() as c:
        c.execute('''INSERT INTO recsys_ab.compatibility_requests(request_key,experiment_id,variant,case_id,release_id,fixture_checksum)
            VALUES(%s,%s,'control','tool_selection',%s,%s)''',
            (key,state['experiment_id'],state['baseline']['release_id'],digest(smoke_fixtures(SAFETY))))
    calls=[];run(db,state,responder(calls))
    assert len(calls)==5
    assert observation(db,state)['verdict']=='HOLD'


def test_stale_offline_identity_rejects_before_any_remaining_calls(state_db):
    db,state=state_db
    with db.connect() as c:
        c.execute('''INSERT INTO recsys_ab.compatibility_requests(request_key,experiment_id,variant,case_id,release_id,fixture_checksum)
            VALUES(%s,%s,'control','tool_selection',%s,'invalid-checksum')''',
            (uuid.uuid4().hex,state['experiment_id'],state['baseline']['release_id']))
    calls=[]
    with pytest.raises(ValueError,match='integrity'):run(db,state,responder(calls))
    assert calls==[]


def test_missing_or_changed_frozen_suite_rejects_before_inference(state_db):
    db,state=state_db; calls=[]
    state['compatibility_fixtures'][0][1][0]['content'] += ' changed'
    with pytest.raises(ValueError,match='frozen compatibility'):
        run(db,state,responder(calls))
    assert calls==[]
