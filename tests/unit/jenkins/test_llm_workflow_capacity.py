from copy import deepcopy
from decimal import Decimal
from pathlib import Path
import pytest
from jenkins.python.llm_agent_cd.capacity import quantity, requests, verify_capacity


def test_reviewed_baseline_repair_is_not_full_experiment_capacity():
    from jenkins.python.llm_agent_cd.capacity_window import (reviewed_window,FAILED_V2,FAILED,
        FAILED_NATIVE_PREP,FAILED_NATIVE_V1)
    assert reviewed_window('reviewed-capacity-baseline-repair-v3')==(FAILED_V2,2,'baseline_repair_only')
    assert reviewed_window('reviewed-capacity-window-v2')==(FAILED,4,'full_experiment')
    assert reviewed_window('reviewed-capacity-native-tools-v4')==(FAILED_NATIVE_PREP,2,'full_experiment_native_tools')
    assert reviewed_window('reviewed-capacity-native-tools-v5')==(FAILED_NATIVE_V1,2,'full_experiment_native_tools_v2')
    from jenkins.python.llm_agent_cd.capacity_window import FAILED_NATIVE_V2
    assert reviewed_window('reviewed-capacity-native-tools-v6')==(FAILED_NATIVE_V2,2,'full_experiment_native_tools_v3')
    from jenkins.python.llm_agent_cd.capacity_window import FAILED_NATIVE_V3
    assert reviewed_window('reviewed-capacity-native-tools-v7')==(FAILED_NATIVE_V3,2,'full_experiment_native_tools_compact')
    from jenkins.python.llm_agent_cd.capacity_window import FAILED_NATIVE_COMPACT
    assert reviewed_window('reviewed-capacity-native-tools-v8')==(FAILED_NATIVE_COMPACT,2,'full_experiment_native_tools_compact_wire')
    from jenkins.python.llm_agent_cd.capacity_window import FAILED_NATIVE_COMPACT_WIRE
    assert reviewed_window('reviewed-capacity-native-tools-v9')==(FAILED_NATIVE_COMPACT_WIRE,2,'full_experiment_native_tools_null_semantics')
    from jenkins.python.llm_agent_cd.capacity_window import FAILED_NATIVE_NULL_SEMANTICS
    assert reviewed_window('reviewed-capacity-native-tools-v10')==(FAILED_NATIVE_NULL_SEMANTICS,2,'full_experiment_native_tools_empty_description')
    with pytest.raises(ValueError,match='unreviewed'):reviewed_window('arbitrary-release')


def test_native_capacity_reserves_control_and_one_adapter_per_pool():
    from jenkins.python.llm_agent_cd.capacity_window import native_reservations
    reservations=native_reservations()
    assert len(reservations)==3
    by_name={r['metadata']['name']:r for r in reservations}
    assert by_name['reserved-qwen35-native-control']['spec']['template']['spec']['nodeSelector']=={
        'recsys.ai/pool':'ml-system'}
    assert by_name['reserved-qwen35-native-control']['spec']['template']['spec']['containers'][0][
        'resources']['requests']=={'cpu':'100m','memory':'1536Mi'}
    assert {r['spec']['template']['spec']['nodeSelector']['recsys.ai/pool'] for n,r in by_name.items()
            if n.startswith('reserved-native-adapter-')}=={'ml-system','cpu-services'}
    assert len(native_reservations(include_backend=False))==2


def test_active_capacity_window_targets_only_one_replica_retention():
    from jenkins.python.llm_agent_cd.active_capacity_window import CHAMPION, REVIEW, TARGETS
    assert REVIEW == 'reviewed-active-history-rightsize-v11'
    assert CHAMPION not in TARGETS.values()
    assert len(TARGETS) == 3 and all(len(release_id) == 64 for release_id in TARGETS.values())


def test_candidate_release_recovery_requires_zero_inference_evidence():
    from jenkins.python.llm_agent_cd.candidate_release_recovery import (validate_recovery,
        EXPERIMENT, RELEASE)
    state = {'phase':'ROLLED_BACK','experiment_id':EXPERIMENT,
        'pending':{'release_id':RELEASE},'champion':{'release_id':'control'},
        'disabled':[RELEASE],'gate':{'verdict':'FAIL','reason':'execution error: ValueError'},
        'cases':{},'offline_evidence':{}}
    assert validate_recovery(state, {'invocations':0,'compatibility':0,'sessions':0}) == 'pending'
    state['disabled'] = []
    state['recovery_events'] = [{'review':'reviewed-pre-inference-props-newline-v1',
        'failed_experiment':EXPERIMENT,'release_id':RELEASE,'inference_requests':0}]
    assert validate_recovery(state, {'invocations':0,'compatibility':0,'sessions':0}) == 'recovered'
    with pytest.raises(ValueError, match='forbidden'):
        validate_recovery(state, {'invocations':1,'compatibility':0,'sessions':0})
    state['recovery_events'] = []
    with pytest.raises(ValueError, match='without exact recovery evidence'):
        validate_recovery(state, {'invocations':0,'compatibility':0,'sessions':0})


def test_candidate_telemetry_recovery_accepts_only_reviewed_hold_window():
    from jenkins.python.llm_agent_cd.candidate_telemetry_recovery import (
        validate_recovery, EXPERIMENT, RELEASE)
    control='8a62a3d15896940c535aab556e83e6ce00c1c5b98716f6ef079e93a245a425c6'
    state={'phase':'ROLLED_BACK','experiment_id':EXPERIMENT,
        'pending':{'release_id':RELEASE},'baseline':{'release_id':control},
        'champion':{'release_id':control},'disabled':[RELEASE],
        'gate':{'verdict':'FAIL','reason':'operator requested rollback'},'cases':{},
        'offline_evidence':{'cases':[{'variant':variant,'synced':True,'result':{'verdict':'PASS'}}
            for variant in ('control','candidate') for _ in range(3)]}}
    evidence={'invocations':{
        ('live_test',control,'HOLD','child evidence unavailable',False,False):34,
        ('live_test',RELEASE,'HOLD','child evidence unavailable',False,False):3},
        'submitted':37,'compatibility':{('candidate','PASS',True):3,('control','PASS',True):3},
        'offline_confirmed':6}
    assert validate_recovery(state,evidence)=='pending'
    broken=deepcopy(evidence);broken['invocations'][('live_test',RELEASE,'FAIL','bad',False,True)]=1
    with pytest.raises(ValueError,match='37 telemetry-HOLD'):
        validate_recovery(state,broken)


def test_candidate_fixture_recovery_rejects_any_candidate_failure():
    from jenkins.python.llm_agent_cd.candidate_fixture_recovery import (
        validate_recovery,EXPERIMENT,RELEASE,CONTROL)
    state={'phase':'ROLLED_BACK','experiment_id':EXPERIMENT,'pending':{'release_id':RELEASE},
      'champion':{'release_id':CONTROL},'disabled':[RELEASE],'cases':{},
      'gate':{'verdict':'FAIL','reason':'champion runtime/tool contract violation'},
      'offline_evidence':{'cases':[{'variant':v,'synced':True,'result':{'verdict':'PASS'}}
        for v in ('control','candidate') for _ in range(3)]}}
    evidence={'invocations':{
      ('live_test',CONTROL,'FAIL','child runtime error',True,True):2,
      ('live_test',RELEASE,'HOLD','operational evidence incomplete',False,False):1},
      'submitted':3,'compatibility':{('candidate','PASS',True):3,('control','PASS',True):3},
      'offline_confirmed':6}
    assert validate_recovery(state,evidence)=='pending'
    bad=deepcopy(evidence);bad['invocations'][('live_test',RELEASE,'FAIL','runtime',True,False)]=1
    with pytest.raises(ValueError,match='control-failure/candidate-HOLD'):
        validate_recovery(state,bad)


def test_structured_fixture_recovery_requires_one_control_failure_and_no_candidate_call():
    from jenkins.python.llm_agent_cd.candidate_structured_fixture_recovery import (
        validate_recovery,EXPERIMENT,RELEASE,CONTROL)
    state={'phase':'ROLLED_BACK','experiment_id':EXPERIMENT,'pending':{'release_id':RELEASE},
      'champion':{'release_id':CONTROL},'disabled':[RELEASE],'cases':{},
      'gate':{'verdict':'FAIL','reason':'champion runtime/tool contract violation'},
      'offline_evidence':{'cases':[{'variant':v,'synced':True,'result':{'verdict':'PASS'}}
        for v in ('control','candidate') for _ in range(3)]}}
    evidence={'invocations':{('live_test',CONTROL,'FAIL','child runtime error',True,True):1},
      'submitted':1,'compatibility':{('candidate','PASS',True):3,('control','PASS',True):3},
      'offline_confirmed':6}
    assert validate_recovery(state,evidence)=='pending'
    bad=deepcopy(evidence);bad['invocations'][('live_test',RELEASE,'HOLD','operational evidence incomplete',False,False)]=1
    with pytest.raises(ValueError,match='single control-failure/no-candidate'):
        validate_recovery(state,bad)


def test_adapter_placement_capacity_accepts_only_exact_disabled_candidate_state():
    from jenkins.python.llm_agent_cd.adapter_placement_capacity import (
        validate_state, CHAMPION, RELEASE)
    state={'phase':'ROLLED_BACK','champion':{'release_id':CHAMPION},
        'pending':{'release_id':RELEASE},'disabled':[RELEASE]}
    validate_state(state)
    for mutation in (
        lambda s:s.update(phase='CANARY'),
        lambda s:s.update(champion={'release_id':'other'}),
        lambda s:s.update(disabled=[]),
    ):
        broken=deepcopy(state);mutation(broken)
        with pytest.raises(ValueError,match='exact disabled-candidate'):
            validate_state(broken)


def test_previous_placement_capacity_accepts_only_exact_idle_baseline_state():
    from jenkins.python.llm_agent_cd.previous_adapter_capacity import (
        validate_state, CHAMPION, RELEASE, TARGETS)
    state={'phase':'IDLE','activated':True,'champion':{'release_id':CHAMPION},
        'previous':{'release_id':RELEASE}}
    validate_state(state)
    for mutation in (
        lambda s:s.update(phase='CANARY'),
        lambda s:s.update(experiment_id='wf-'+'a'*32),
        lambda s:s.update(previous={'release_id':'other'}),
    ):
        broken=deepcopy(state);mutation(broken)
        with pytest.raises(ValueError,match='exact idle new-baseline'):
            validate_state(broken)
    assert {item['release'] for item in TARGETS}=={CHAMPION,RELEASE}
    assert {item['target_pool'] for item in TARGETS}=={'ml-system','cpu-services'}
    assert len({item['target'] for item in TARGETS})==2
    source=open('jenkins/python/llm_agent_cd/previous_adapter_capacity.py').read()
    assert 'workflow_gateway_replicas' in source
    lines={line.strip() for line in source.splitlines()}
    assert 'reserve += pair + [pair[0]]' in lines
    assert 'final_reserve += pair + [pair[0]]' in lines
    assert 'len(active_router) == 1' in source
    assert 'two Ready Envoy gateways required' in source


def test_current_release_capacity_requires_rollback_and_retains_n2_peers():
    from jenkins.python.llm_agent_cd.current_release_capacity import release_ids
    state = {'phase': 'ROLLED_BACK', 'champion': {'release_id': 'champion'},
        'pending': {'release_id': 'candidate'}, 'disabled': ['candidate']}
    assert release_ids(state) == ('champion', 'candidate')
    for mutation in (
        lambda s: s.update(phase='CANARY'),
        lambda s: s.update(disabled=[]),
        lambda s: s.update(pending={'release_id': 'champion'}),
    ):
        broken = deepcopy(state); mutation(broken)
        with pytest.raises(ValueError, match='exact champion/rolled-back-candidate'):
            release_ids(broken)
    source = Path('jenkins/python/llm_agent_cd/current_release_capacity.py').read_text()
    assert 'deployment(base + "-cpu", "cpu-services", release_id)' in source
    assert 'native_reservations(include_backend=False)' in source
    assert 'admitted workflow backend is not Ready' in source
    assert 'retained_ready_backends' in source
    assert 'headroom={"cpu": "200m", "memory": "128Mi"}' in source


def test_idle_previous_capacity_accepts_only_activated_idle_state():
    from jenkins.python.llm_agent_cd.idle_previous_adapter_capacity import (
        REVIEW, validate_state)

    champion_id, release_id = "b" * 64, "a" * 64
    state = {"phase": "IDLE", "activated": True,
        "champion": {"release_id": champion_id},
        "previous": {"release_id": release_id}}
    assert validate_state(state) == (champion_id, release_id)
    assert REVIEW == "reviewed-idle-previous-single-adapter-v1"
    for mutation in (
        lambda value: value.update(phase="CANARY"),
        lambda value: value.update(activated=False),
        lambda value: value.update(experiment_id="wf-" + "b" * 32),
        lambda value: value.update(previous={"release_id": "not-a-release"}),
    ):
        broken = deepcopy(state)
        mutation(broken)
        with pytest.raises(ValueError, match="exact activated IDLE"):
            validate_state(broken)


def test_finite_jobs_are_reserved_once_with_probe_and_sequential_worker():
    from jenkins.python.llm_agent_cd.capacity import workflow_job_reservations
    assert len(workflow_job_reservations([],probe=True))==4
    pod={'metadata':{'namespace':'kagent','ownerReferences':[{'kind':'Job','name':'recsys-workflow-evaluation-123'}]},
         'spec':{'nodeName':'e2'},'status':{'phase':'Running'}}
    assert len(workflow_job_reservations([pod],probe=True))==3
    with pytest.raises(ValueError,match='overlap'):workflow_job_reservations([pod,pod])
    pod['status']['phase']='Succeeded'
    assert len(workflow_job_reservations([pod],probe=True))==4


def setup():
    node = {"metadata": {"name": "cpu", "labels": {"pool": "cpu"}}, "spec": {}, "status": {
        "conditions": [{"type": "Ready", "status": "True"}],
        "allocatable": {"cpu": "4", "memory": "8Gi", "pods": "100", "ephemeral-storage": "10Gi"}}}
    spec = {"containers": [{"name": "model", "resources": {"requests": {"cpu": "2", "memory": "3Gi"}}}]}
    deploy = {"metadata": {"name": "candidate", "namespace": "kagent"}, "spec": {"replicas": 1, "template": {"spec": spec}}}
    return node, spec, deploy


def test_quantity_and_init_sidecar_overhead():
    assert quantity("300m") == Decimal(".3")
    assert quantity("1.5Gi") == Decimal(1610612736)
    spec = {"containers": [{"resources": {"requests": {"cpu": "1"}}}], "initContainers": [
        {"restartPolicy": "Always", "resources": {"requests": {"cpu": "200m"}}},
        {"resources": {"requests": {"cpu": "2"}}}], "overhead": {"cpu": "100m"}}
    assert requests(spec)["cpu"] == Decimal("2.3")


@pytest.mark.parametrize("cause", ["cpu", "memory", "taint", "selector", "pressure", "slots", "affinity"])
def test_insufficient_candidate_capacity_fails_before_deploy(cause):
    node, spec, deploy = setup()
    if cause == "cpu": node["status"]["allocatable"]["cpu"] = "1900m"
    if cause == "memory": node["status"]["allocatable"]["memory"] = "2Gi"
    if cause == "taint": node["spec"]["taints"] = [{"key": "pool", "value": "ml", "effect": "NoSchedule"}]
    if cause == "selector": spec["nodeSelector"] = {"pool": "other"}
    if cause == "pressure": node["status"]["conditions"].append({"type": "MemoryPressure", "status": "True"})
    if cause == "slots": node["status"]["allocatable"]["pods"] = "0"
    if cause == "affinity": spec["affinity"] = {"nodeAffinity": {}}
    with pytest.raises(ValueError, match="capacity"):
        verify_capacity([node], [], [deploy], set())


def test_existing_requests_and_all_candidate_replicas_are_reserved():
    node, spec, deploy = setup()
    pod = {"spec": {**deepcopy(spec), "nodeName": "cpu"}, "status": {"phase": "Running"}}
    verify_capacity([node], [pod], [deploy], set())
    deploy["spec"]["replicas"] = 2
    with pytest.raises(ValueError, match="capacity"):
        verify_capacity([node], [pod], [deploy], set())
    # An immutable shared deployment already present does not allocate it again.
    verify_capacity([node], [pod], [deploy], {("kagent", "candidate")})


def test_completed_pods_release_capacity_but_terminating_pods_do_not():
    node, spec, deploy = setup()
    pod = {"metadata": {"deletionTimestamp": "now"}, "spec": {**deepcopy(spec), "nodeName": "cpu"}, "status": {"phase": "Running"}}
    deploy["spec"]["replicas"] = 2
    with pytest.raises(ValueError): verify_capacity([node], [pod], [deploy], set())
    pod["status"]["phase"] = "Succeeded"
    verify_capacity([node], [pod], [deploy], set())


def test_single_selector_domain_spread_is_checked_without_mutating_spec():
    node,spec,deploy=setup()
    node['metadata']['labels']['kubernetes.io/hostname']='cpu'
    spec['nodeSelector']={'pool':'cpu'}
    spec['topologySpreadConstraints']=[{'topologyKey':'kubernetes.io/hostname','maxSkew':1,'whenUnsatisfiable':'DoNotSchedule'}]
    original=deepcopy(spec)
    verify_capacity([node],[],[deploy],{},headroom={'cpu':'200m'})
    assert spec==original
    spec['topologySpreadConstraints'][0]['minDomains']=2
    with pytest.raises(ValueError,match='topology'): verify_capacity([node],[],[deploy],{})
