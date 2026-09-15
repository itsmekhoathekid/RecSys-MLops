from copy import deepcopy
import pytest
from tests.unit.jenkins.test_llm_workflow import bundle,champion
from tests.unit.jenkins.test_llm_agent_cd import MemoryStore,FakeDriver
from jenkins.python.llm_agent_cd.baseline_cutover import begin,reconcile
from jenkins.python.llm_agent_cd.workflow_contract import revise_baseline


@pytest.mark.parametrize('verdict',[None,'FAIL','PASS'])
def test_later_serving_probe_cannot_be_bypassed_with_older_preparation(verdict):
    import io,json
    from types import SimpleNamespace
    from botocore.exceptions import ClientError
    from jenkins.python.llm_agent_cd.baseline_cutover import check_stock_probes
    def get_object(**kwargs):
        if verdict is None:raise ClientError({'Error':{'Code':'NoSuchKey'}},'GetObject')
        key=kwargs['Key']
        role=('recommendation' if 'stock-recommendation' in key else
              'context' if 'stock-context' in key else 'coordinator')
        return {'Body':io.BytesIO(json.dumps({'verdict':verdict,'release_id':'baseline','role':role}).encode())}
    store=SimpleNamespace(client=SimpleNamespace(get_object=get_object),bucket='test')
    if verdict=='PASS':check_stock_probes(store,{'release_id':'baseline'})
    else:
        with pytest.raises(ValueError,match='compatibility'):check_stock_probes(store,{'release_id':'baseline'})


def setup(bundle):
    new,_=revise_baseline(bundle)
    store=MemoryStore(bundle);driver=FakeDriver();driver.kube=lambda *a,**kw:''
    prepared={'baseline':new,'parent_state_etag':0,'old_champion':bundle['release_id']}
    return store,driver,prepared


def test_cutover_crash_after_apply_preserves_intent_old_champion_and_never_infers(bundle):
    store,driver,prepared=setup(bundle)
    begin(store,driver,prepared,lambda:None)
    assert store.value['champion']==bundle and not driver.weights
    driver.ack=False
    assert not reconcile(store,driver,prepared,lambda:None)
    assert store.value['phase']=='BASELINE_CUTOVER'
    # Jenkins may restart here; same preparation is resumed, never bootstrapped.
    begin(store,driver,prepared,lambda:None)
    driver.ack=True
    assert reconcile(store,driver,prepared,lambda:None)
    assert store.value['champion']==prepared['baseline'] and store.value['activated']
    assert store.value['baseline_cutover']['old_bootstrap']==bundle
    assert driver.calls==[]


def test_stale_preparation_and_foreign_route_fail_before_intent(bundle):
    store,driver,prepared=setup(bundle)
    prepared['parent_state_etag']=1
    with pytest.raises(ValueError,match='STALE'):begin(store,driver,prepared,lambda:None)
    prepared['parent_state_etag']=0
    driver.kube=lambda *a,**kw:'existing-route'
    with pytest.raises(ValueError,match='existing'):begin(store,driver,prepared,lambda:None)
    assert store.value['phase']=='IDLE' and driver.weights==[]


def test_global_drift_during_cutover_leaves_closed_intent(bundle):
    store,driver,prepared=setup(bundle)
    begin(store,driver,prepared,lambda:None)
    def stale():raise ValueError('STALE_SNAPSHOT')
    with pytest.raises(ValueError,match='STALE'):reconcile(store,driver,prepared,stale)
    assert store.value['phase']=='BASELINE_CUTOVER' and not store.value.get('activated')
    assert driver.weights==[]


def test_cutover_lost_cas_cannot_commit_champion(bundle):
    store,driver,prepared=setup(bundle)
    begin(store,driver,prepared,lambda:None)
    def concurrent(*a):
        store.etag+=1
        return True
    driver.verify_route=concurrent
    with pytest.raises(RuntimeError,match='CAS'):reconcile(store,driver,prepared,lambda:None)
    assert store.value['champion']==bundle


def active_stock_revision(bundle):
    import json
    from pathlib import Path
    from jenkins.python.llm_agent_cd.release import release
    from jenkins.python.llm_agent_cd.workflow_contract import revise_stock_runtime_and_b8646_serving
    raw={k:deepcopy(bundle[k]) for k in ('schema_version','scope','global_generation','agent_overrides','llm','agents','bindings')}
    llm=json.loads(Path('configs/llm-ab/catalog/qwen3.5-0.8b-q4_0-b8646-stock-adk-v1.json').read_text())
    raw['llm']=deepcopy(llm)
    for agent in raw['agents'].values():
        agent['systemMessage'] += '\nRuntime model configuration revision: stock-test.\n'
    old=release(raw)
    new,audit=revise_stock_runtime_and_b8646_serving(
        old,llm,'registry/golang-adk@sha256:'+'b'*64,
        'registry/router@sha256:'+'d'*64)
    return old,new,audit


def test_active_stock_cutover_archives_failure_and_preserves_old_sessions(bundle):
    old,new,audit=active_stock_revision(bundle)
    store=MemoryStore(old);driver=FakeDriver();driver.kube=lambda *a,**kw:'existing-route'
    store.value.update(activated=True,phase='ROLLED_BACK',experiment_id='old-failed',
        verified_weight=0,route_revision='old-route',baseline=old,pending=old,releases={old['release_id']:old})
    archives=[]
    def archive(state):
        assert state['phase']=='ROLLED_BACK' and state['champion']==old
        archives.append(state['experiment_id'])
        return {'experiment_id':'old-failed','key':'old-archive'}
    store.archive=archive
    prepared={'migration_kind':audit['change_type'],'audit':audit,'baseline':new,
        'parent_state_etag':0,'old_champion':old['release_id']}
    begin(store,driver,prepared,lambda:None)
    assert store.value['champion']==old and archives==['old-failed']
    assert reconcile(store,driver,prepared,lambda:None)
    assert store.value['champion']==new and store.value['previous']==old
    assert old['release_id'] in store.value['releases'] and 'experiment_id' not in store.value
    assert driver.calls==[]


def test_active_revision_rejects_tampered_audit(bundle):
    old,new,audit=active_stock_revision(bundle)
    store=MemoryStore(old);driver=FakeDriver();driver.kube=lambda *a,**kw:'existing-route'
    store.value.update(activated=True,phase='ROLLED_BACK',verified_weight=0,route_revision='old-route')
    prepared={'migration_kind':audit['change_type'],'audit':{**audit,'generation_and_overrides_unchanged':False},
        'baseline':new,'parent_state_etag':0,'old_champion':old['release_id']}
    with pytest.raises(ValueError,match='evidence mismatch'):
        begin(store,driver,prepared,lambda:None)


def test_active_coordinator_prompt_revision_is_reproduced_and_preserves_previous(bundle):
    from jenkins.python.llm_agent_cd.workflow_contract import revise_coordinator_terminal_prompt

    _legacy, old, _old_audit = active_stock_revision(bundle)
    new, audit = revise_coordinator_terminal_prompt(old)
    store=MemoryStore(old);driver=FakeDriver();driver.kube=lambda *a,**kw:'existing-route'
    store.value.update(activated=True,phase='IDLE',verified_weight=0,
        route_revision='old-route',baseline=old,pending=old,releases={old['release_id']:old})
    prepared={'migration_kind':audit['change_type'],'audit':audit,'baseline':new,
        'parent_state_etag':0,'old_champion':old['release_id']}
    begin(store,driver,prepared,lambda:None)
    assert store.value['champion']==old
    assert reconcile(store,driver,prepared,lambda:None)
    assert store.value['champion']==new and store.value['previous']==old


def test_active_native_sequential_revision_is_reproduced_and_preserves_previous(bundle):
    from jenkins.python.llm_agent_cd.workflow_contract import (
        revise_coordinator_terminal_prompt,
        revise_coordinator_native_sequential_baseline,
    )

    _legacy, stock, _old_audit = active_stock_revision(bundle)
    old, _prompt_audit = revise_coordinator_terminal_prompt(stock)
    new, audit = revise_coordinator_native_sequential_baseline(old)
    store=MemoryStore(old);driver=FakeDriver();driver.kube=lambda *a,**kw:'existing-route'
    store.value.update(activated=True,phase='IDLE',verified_weight=0,
        route_revision='old-route',baseline=old,pending=old,releases={old['release_id']:old})
    prepared={'migration_kind':audit['change_type'],'audit':audit,'baseline':new,
        'parent_state_etag':0,'old_champion':old['release_id']}
    begin(store,driver,prepared,lambda:None)
    assert store.value['champion']==old
    assert reconcile(store,driver,prepared,lambda:None)
    assert store.value['champion']==new and store.value['previous']==old
    assert all('isolateSessions' not in tool
               for tool in new['agents']['coordinator']['tools'])
