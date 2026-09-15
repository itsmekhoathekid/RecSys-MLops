from types import SimpleNamespace as NS
import pytest
from apps.agentic.llm_ab_router.child_tasks import ChildTasks
from jenkins.python.llm_agent_cd.workflow_evidence import tool_value

SID='12345678-1234-1234-1234-123456789abc'


def reader(**overrides):
    r=object.__new__(ChildTasks)
    r.metadata=(('x-user-id','trusted-user'),)
    r.agent_ids=frozenset({'immutable-context'})
    r.pb=NS(SESSION_SOURCE_AGENT=2,SESSION_SOURCE_UNSPECIFIED=0,
        GetSessionRequest=lambda **kw:kw,ListTasksRequest=lambda **kw:kw)
    session=NS(**dict({'id':SID,'user_id':'trusted-user','source':0,'agent_id':'immutable-context'},**overrides))
    r.sessions=NS(GetSession=lambda *a,**kw:NS(session=session))
    r.tasks=NS(ListTasks=lambda *a,**kw:NS(tasks=[{'contextId':SID}]))
    r.to_dict=lambda value:value
    return r


def test_nil_substrate_source_requires_exact_user_and_immutable_agent():
    assert reader()(SID)['result']['task']['contextId']==SID


@pytest.mark.parametrize('bad',[{'user_id':'other'},{'agent_id':'unrelated'},{'source':1},{'id':'wrong'}])
def test_child_identity_rejects_foreign_context(bad):
    with pytest.raises(ValueError,match='identity'):reader(**bad)(SID)


def test_fastmcp_envelope_does_not_drop_business_metadata():
    payload={'user_id':218,'items':[],'metadata':{'revision':'source'}}
    assert tool_value({'structuredContent':{'output':payload}})==payload
    raw={'output':payload,'metadata':'preserve'}
    assert tool_value(raw)==raw


def test_inline_transport_task_requires_session_identity_and_context():
    r=reader()
    r.tasks=NS(ListTasks=lambda *a,**kw:pytest.fail('inline evidence must not require empty task store'))
    task={'id':'actual-task','contextId':SID}
    assert r(SID,task)['result']['task']==task
    with pytest.raises(ValueError,match='context'):r(SID,{'contextId':'foreign'})
    with pytest.raises(ValueError,match='identity'):reader(user_id='foreign')(SID,task)
