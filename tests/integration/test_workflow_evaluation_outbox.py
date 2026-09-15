"""PostgreSQL integration, no real agent, model, Langfuse or production calls."""
import os
import uuid
from copy import deepcopy
import pytest
from apps.agentic.llm_ab_router.database import Database
from apps.agentic.llm_ab_router.evaluation_job import drain
from tests.unit.jenkins.test_code_evaluation import request
from tests.unit.jenkins.test_llm_workflow import bundle, champion

DSN = os.environ.get('LLM_AB_TEST_DATABASE_URL','')
pytestmark = pytest.mark.skipif(not DSN,reason='disposable localhost PostgreSQL required')


@pytest.fixture
def db():
    assert '127.0.0.1' in DSN or 'localhost' in DSN
    db=Database(DSN);db.migrate()
    with db.connect() as c:
        c.execute('TRUNCATE recsys_ab.evaluation_outbox, recsys_ab.invocations, recsys_ab.sessions')
    return db


def seed(db,bundle):
    key=uuid.uuid4().hex
    with db.connect() as c:
        c.execute("INSERT INTO recsys_ab.sessions(session_key,release_id,backend_context) VALUES(%s,'release','context')",(key,))
        c.execute("INSERT INTO recsys_ab.invocations(request_key,session_key,experiment_id,source,release_id) VALUES(%s,%s,'experiment','synthetic','release')",(key,key))
    ev={'snapshot':request(bundle),'metadata':{'experiment_id':'experiment','request_key':key,'trace_id':'a'*32}}
    return key,ev


def test_completed_response_and_outbox_atomic_and_conflict_rolls_back(db,bundle):
    key,ev=seed(db,bundle)
    db.complete(key,{'verdict':'PASS'},{'original':True},'task',evaluation=ev)
    changed=deepcopy(ev);changed['snapshot']['duration_seconds']=999
    with pytest.raises(ValueError):
        db.complete(key,{'verdict':'FAIL'},{'original':False},'changed-task',evaluation=changed)
    with db.connect() as c:
        row=c.execute('SELECT result,response FROM recsys_ab.invocations WHERE request_key=%s',(key,)).fetchone()
    assert row['result']['verdict']=='PASS' and row['response']=={'original':True}


def test_delivery_retry_reuses_score_ids_and_never_reexecutes_workflow(db,bundle):
    key,ev=seed(db,bundle)
    db.complete(key,{'verdict':'PASS'},{'done':True},'task',evaluation=ev)
    class FakeSync:
        calls=[]
        fail=True
        def confirm(self,payload):
            self.calls.append(payload['id'])
            if self.fail: raise ValueError('score_not_visible')
    sync=FakeSync()
    assert drain(db,sync,seconds=5)==0
    assert db.result(key)['evaluation']['synced'] is False
    with db.connect() as c:
        c.execute('UPDATE recsys_ab.evaluation_outbox SET next_attempt_at=now() WHERE request_key=%s',(key,))
    sync.fail=False
    assert drain(db,sync,seconds=5)==1
    half=len(sync.calls)//2
    assert sync.calls[:half]==sync.calls[half:]
    from jenkins.python.llm_agent_cd.code_evaluation import VERSION
    assert db.result(key)['evaluation']=={'verdict':'PASS','evaluator_version':VERSION,'synced':True}
    assert drain(db,sync,seconds=5)==0
    with db.connect() as c:
        assert c.execute('SELECT count(*) AS n FROM recsys_ab.invocations').fetchone()['n']==1


def test_live_load_owner_is_durable_and_budget_never_exceeds_360(db):
    from concurrent.futures import ThreadPoolExecutor
    eid='wf-'+uuid.uuid4().hex
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert sum(pool.map(lambda _: db.claim_live_load(eid),range(4)))==1
    assert Database(DSN).claim_live_load(eid) is False
    for expected in range(1,361):
        assert db.claim_live_request(eid)==expected
    assert db.claim_live_request(eid) is None
    assert db.claim_live_load(eid) is False


def test_synthetic_suite_completion_and_inflight_are_exact(db):
    eid = 'rec-' + uuid.uuid4().hex
    with db.connect() as c:
        for index in range(20):
            key = uuid.uuid4().hex
            c.execute(
                "INSERT INTO recsys_ab.sessions(session_key,release_id,backend_context) VALUES(%s,'release',%s)",
                (key, key),
            )
            c.execute(
                """INSERT INTO recsys_ab.invocations
                   (request_key,session_key,experiment_id,source,release_id,finished_at)
                   VALUES(%s,%s,%s,'synthetic','release',CASE WHEN %s THEN now() END)""",
                (key, key, eid, index < 19),
            )
    assert db.source_inflight(eid, 'synthetic') == 1
    assert db.synthetic_suite_complete(eid, 20) is False
    with db.connect() as c:
        c.execute(
            "UPDATE recsys_ab.invocations SET finished_at=now() WHERE experiment_id=%s",
            (eid,),
        )
    assert db.source_inflight(eid, 'synthetic') == 0
    assert db.synthetic_suite_complete(eid, 20) is True
