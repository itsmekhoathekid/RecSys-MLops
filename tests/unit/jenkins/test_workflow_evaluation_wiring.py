from copy import deepcopy
import json
from tests.unit.jenkins.test_llm_workflow import bundle, champion, candidate
from jenkins.python.llm_agent_cd.offline import job


def test_preparation_artifact_is_reset_and_failure_cannot_archive_old_success():
    from pathlib import Path
    source=Path('jenkins/LLMWorkflowEvaluationPrepare.Jenkinsfile').read_text()
    assert source.index("stage: 'NOT_STARTED'")<source.index("writeFile file: 'workflow-preparation.tgz'")
    post=source.split('  post {',1)[1]
    assert "currentBuild.currentResult != 'SUCCESS'" in post
    assert post.index("stage: 'PREPARATION_FAILED'")<post.index('archiveArtifacts')
    assert 'build_url: env.BUILD_URL' in post and 'build_number: env.BUILD_NUMBER' in post


def test_preparation_bundle_contains_import_dependencies_and_no_credentials():
    import io
    import tarfile
    from jenkins.python.llm_agent_cd.evaluation_prepare import bundle, groovy_base64
    with tarfile.open(fileobj=io.BytesIO(bundle()),mode='r:gz') as archive:
        names=archive.getnames()
    assert 'jenkins/python/model_cd/storage.py' in names
    assert 'configs/agentic/recsys-context-agent/tools-contract.json' in names
    assert all(not n.startswith(('.llm-agent-cd','.git/')) and '__pycache__' not in n for n in names)
    encoded=groovy_base64(b'a'*100000)
    assert max(len(s) for s in encoded.split("','"))<17000


def test_context_contract_deploy_has_a_distinct_locked_jenkins_job():
    from inspect import signature
    from pathlib import Path
    from jenkins.python.llm_agent_cd import evaluation_prepare

    assert "context_contract" in signature(evaluation_prepare.install).parameters
    installer = Path("jenkins/python/llm_agent_cd/evaluation_prepare.py").read_text()
    deploy = Path("jenkins/python/llm_agent_cd/context_contract_deploy.py").read_text()
    assert "RecSys-Workflow-Context-Contract-v2" in installer
    assert "llm_agent_cd.context_contract_deploy" in installer
    assert "recsys-production-release" in Path(
        "jenkins/LLMWorkflowEvaluationPrepare.Jenkinsfile"
    ).read_text()
    assert "IfNoneMatch=\"*\"" in deploy
    assert "mcp_schema_round_trip_verified" in deploy


def test_historical_single_placement_capacity_retains_sessions_and_peer():
    from inspect import signature
    from pathlib import Path
    from jenkins.python.llm_agent_cd import evaluation_prepare

    assert "sessionless_adapter_capacity" in signature(
        evaluation_prepare.install
    ).parameters
    installer = Path("jenkins/python/llm_agent_cd/evaluation_prepare.py").read_text()
    capacity = Path(
        "jenkins/python/llm_agent_cd/sessionless_adapter_capacity.py"
    ).read_text()
    assert "RecSys-Workflow-Sessionless-History-Capacity" in installer
    assert "815fe04ba955a000dbe627f2ac0ba107" in capacity
    assert 'if unfinished:' in capacity
    assert "sessions_retained" in capacity
    assert "retained historical peer has no Ready Service endpoint" in capacity
    assert "release_backend_and_evidence_retained" in capacity
    assert '"delete"' not in capacity.lower()


def test_stock_acceptance_capacity_keeps_pointer_peers_and_session_data():
    from inspect import signature
    from pathlib import Path
    from jenkins.python.llm_agent_cd import evaluation_prepare

    assert "acceptance_capacity" in signature(evaluation_prepare.install).parameters
    installer = Path("jenkins/python/llm_agent_cd/evaluation_prepare.py").read_text()
    capacity = Path("jenkins/python/llm_agent_cd/acceptance_capacity.py").read_text()
    assert "RecSys-Workflow-Stock-Acceptance-Capacity" in installer
    assert "retained_pointer_peers" in capacity
    assert "sessionless history gained a session" in capacity
    assert "retained pointer service has no Ready endpoint" in capacity
    assert "full_stock_baseline_capacity_verified" in capacity
    assert '"delete"' not in capacity.lower()


def test_offline_job_never_retries_inference(bundle):
    from jenkins.python.llm_agent_cd.release import digest
    from jenkins.python.llm_agent_cd.small_compatibility import smoke_fixtures
    from jenkins.python.llm_agent_cd.workflow_contract import SAFETY
    fixtures=smoke_fixtures(SAFETY)
    state={'experiment_id':'wf-'+'a'*32,'baseline':bundle,'pending':candidate(bundle,llm=True),
        'compatibility_fixtures':fixtures,'compatibility_fixture_checksum':digest(fixtures)}
    value=job(state,'registry/runtime@sha256:'+'d'*64)
    assert value['spec']['backoffLimit']==0
    pod=value['spec']['template']['spec']
    assert pod['restartPolicy']=='Never' and pod['nodeSelector']=={'recsys.ai/pool':'ml-system'}
    assert pod['containers'][0]['envFrom'] == [
        {'secretRef': {'name': 'recsys-workflow-runtime'}}
    ]
    frozen=json.loads(next(e['value'] for e in pod['containers'][0]['env']
        if e['name']=='AB_OFFLINE_MANIFESTS'))
    assert frozen['compatibility_fixtures']==json.loads(json.dumps(fixtures))
    assert frozen['compatibility_fixture_checksum']==digest(fixtures)


def test_recommendation_offline_job_uses_recommendation_runtime_secret():
    from tests.unit.jenkins.test_llm_agent_cd import champion as recommendation_champion
    baseline = recommendation_champion.__wrapped__()
    state = {
        'experiment_id': 'rec-' + 'b' * 32,
        'baseline': baseline,
        'pending': baseline,
        'compatibility_fixtures': [],
        'compatibility_fixture_checksum': '',
    }
    value = job(state, 'registry/runtime@sha256:' + 'd' * 64)
    pod = value['spec']['template']['spec']
    assert pod['containers'][0]['envFrom'] == [
        {'secretRef': {'name': 'recsys-llm-ab-runtime'}}
    ]


def test_coordinator_only_jobs_are_locked_and_do_not_change_traffic():
    from inspect import signature
    from pathlib import Path
    from jenkins.python.llm_agent_cd import evaluation_prepare

    parameters = signature(evaluation_prepare.install).parameters
    assert 'coordinator_worker_capacity' in parameters
    assert 'coordinator_prompt_migration' in parameters
    assert 'prompt_ab_adapter_capacity' in parameters
    assert 'coordinator_candidate_a2a_preflight' in parameters
    assert 'failed_coordinator_candidate_quarantine' in parameters
    installer = Path('jenkins/python/llm_agent_cd/evaluation_prepare.py').read_text()
    capacity = Path('jenkins/python/llm_agent_cd/coordinator_worker_capacity.py').read_text()
    preflight = Path(
        'jenkins/python/llm_agent_cd/coordinator_candidate_a2a_preflight.py'
    ).read_text()
    assert 'RecSys-Workflow-Coordinator-Worker-Capacity' in installer
    assert 'RecSys-Workflow-Coordinator-Prompt-Baseline' in installer
    assert 'RecSys-Workflow-Prompt-AB-Adapter-Capacity' in installer
    assert '--coordinator-prompt-migration' in installer
    assert 'RecSys-Workflow-Coordinator-Candidate-Preflight' in installer
    assert 'RecSys-Workflow-Failed-Coordinator-Candidate-Quarantine' in installer
    assert "'target_role': 'coordinator'" in capacity.replace('"', "'")
    assert 'driver.preflight' in capacity and 'inference_requests": 0' in capacity
    assert 'readonly_candidate_a2a_probes' in preflight
    assert 'candidate_weight": 0' in preflight
    assert 'specialist_llms_unchanged": True' in preflight
    assert 'recsys-production-release' in Path(
        'jenkins/LLMWorkflowEvaluationPrepare.Jenkinsfile').read_text()
    quarantine = Path(
        'jenkins/python/llm_agent_cd/failed_coordinator_candidate_quarantine.py'
    ).read_text()
    assert '2c134fb8697c8b78d12fd8fa2c9f75ab626d9a5b9d091f4d3d117b14aa94be05' in quarantine
    assert 'online_ab_requests": 0' in quarantine
    assert 'taskstore_and_minio_evidence_retained": True' in quarantine


def test_specialists_are_published_before_coordinator_without_identity_changes(bundle):
    from jenkins.python.llm_agent_cd.manifests import resources
    from jenkins.python.llm_agent_cd.release import digest
    objects=resources(bundle,'kagent','registry@sha256:'+'a'*64,'secret')
    assert [o['metadata']['labels']['recsys.ai/agent-role'] for o in objects if o['kind']=='SandboxAgent']==[
        'context','recommendation','coordinator']
    assert all(o['metadata']['annotations']['recsys.ai/immutable-spec']==digest(o['spec']) for o in objects)


def test_collector_plan_only_reserves_and_restarts_on_actual_diff():
    from jenkins.python.llm_agent_cd.evaluation_deploy import collector_metadata_plan
    value={'data':{'config.yaml':'["langfuse.observation.metadata.operation", "existing.key"]'}}
    key,content,changed=collector_metadata_plan(value)
    assert changed and key=='config.yaml' and '"existing.key"' in content
    same_key,same_content,changed=collector_metadata_plan({'data':{key:content}})
    assert not changed and (same_key,same_content)==(key,content)


def test_terminal_router_maintenance_is_no_surge_and_preserves_trigger_image():
    from pathlib import Path
    runtime=Path('infra/helm/recsys-workflow-ab/templates/runtime.yaml').read_text()
    trigger=Path('infra/helm/recsys-workflow-ab/templates/trigger.yaml').read_text()
    evaluation=Path('infra/helm/recsys-workflow-ab/templates/evaluation.yaml').read_text()
    deploy=Path('jenkins/python/llm_agent_cd/evaluation_deploy.py').read_text()
    assert 'rollingUpdate: {maxSurge: 0, maxUnavailable: 1}' in runtime
    assert '$triggerImage := .Values.trigger.image | default .Values.image' in trigger
    assert trigger.count('image: {{ $triggerImage | quote }}') == 2
    assert '$evaluationImage := .Values.evaluation.image | default .Values.image' in evaluation
    assert 'image: {{ $evaluationImage | quote }}' in evaluation
    assert "trigger.image='+trigger_image" in deploy
    assert "no-surge router maintenance requires every live replica Ready" in deploy
    assert "'--set','replicas='+str(router_replicas)" in deploy


def test_trigger_overlay_pins_the_runtime_aware_release_parser_everywhere():
    from pathlib import Path
    deploy = Path('jenkins/python/llm_agent_cd/trigger_deploy.py').read_text()
    trigger = Path('apps/agentic/llm_ab_router/trigger.py').read_text()
    chart = Path('infra/helm/recsys-workflow-ab/templates/trigger.yaml').read_text()
    for module in ('release.py', 'workflow.py', 'serving_profiles.py'):
        assert module in deploy
        assert f'subPath: {module}' in chart
        assert f'"subPath": "{module}"' in trigger


def test_structured_fixture_recovery_has_a_distinct_locked_jenkins_job():
    from inspect import signature
    from jenkins.python.llm_agent_cd import evaluation_prepare
    assert 'candidate_structured_fixture_recovery' in signature(evaluation_prepare.install).parameters
    source=open('jenkins/python/llm_agent_cd/evaluation_prepare.py').read()
    assert 'RecSys-Workflow-Candidate-Structured-Fixture-Recovery' in source
    assert 'llm_agent_cd.candidate_structured_fixture_recovery' in source


def test_runtime_parser_recovery_is_exact_lock_protected_and_no_inference():
    from inspect import signature
    from pathlib import Path
    from jenkins.python.llm_agent_cd import evaluation_prepare
    assert 'candidate_runtime_parser_recovery' in signature(evaluation_prepare.install).parameters
    installer = Path('jenkins/python/llm_agent_cd/evaluation_prepare.py').read_text()
    recovery = Path('jenkins/python/llm_agent_cd/candidate_runtime_parser_recovery.py').read_text()
    assert 'RecSys-Workflow-Candidate-Runtime-Parser-Recovery' in installer
    assert 'llm_agent_cd.candidate_runtime_parser_recovery' in installer
    assert 'wf-33bf2ca18b5ff6564d9ed2a16d5dc759' in recovery
    assert 'bbeb108f9fe941bc5b08de67f14b00c405c51a61a1cb7b698724d33eba4d15c6' in recovery
    assert 'candidate_sessions' in recovery and 'synthetic' in recovery
    assert 'inference_requests": 0' in recovery
    assert 'SendMessage' not in recovery


def test_offline_wiring_recovery_is_exact_and_cannot_hide_online_traffic():
    from inspect import signature
    from pathlib import Path
    from jenkins.python.llm_agent_cd import evaluation_prepare

    assert 'candidate_offline_wiring_recovery' in signature(
        evaluation_prepare.install).parameters
    installer = Path('jenkins/python/llm_agent_cd/evaluation_prepare.py').read_text()
    recovery = Path(
        'jenkins/python/llm_agent_cd/candidate_offline_wiring_recovery.py').read_text()
    assert 'RecSys-Workflow-Candidate-Offline-Wiring-Recovery' in installer
    assert 'llm_agent_cd.candidate_offline_wiring_recovery' in installer
    assert 'wf-ea32d7f844ca548526cb6b0f11415530' in recovery
    assert 'live_load_rows' in recovery and 'candidate_sessions' in recovery
    assert '"inference_requests": 0' in recovery
    assert 'SendMessage' not in recovery


def test_adapter_placement_capacity_has_a_distinct_locked_jenkins_job():
    from inspect import signature
    from jenkins.python.llm_agent_cd import evaluation_prepare
    assert 'adapter_placement_capacity' in signature(evaluation_prepare.install).parameters
    source=open('jenkins/python/llm_agent_cd/evaluation_prepare.py').read()
    assert 'RecSys-Workflow-Adapter-Placement-Capacity' in source
    assert 'llm_agent_cd.adapter_placement_capacity' in source
    assert 'previous_placement_capacity' in signature(evaluation_prepare.install).parameters
    assert 'current_release_capacity' in signature(evaluation_prepare.install).parameters
    assert 'RecSys-Workflow-Previous-Placement-Capacity' in source
    assert 'llm_agent_cd.previous_adapter_capacity' in source


def test_stock_b8646_baseline_has_distinct_locked_promotable_job():
    from inspect import signature
    from jenkins.python.llm_agent_cd import evaluation_prepare
    assert 'stock_b8646_serving' in signature(evaluation_prepare.install).parameters
    source = open('jenkins/python/llm_agent_cd/evaluation_prepare.py').read()
    assert 'RecSys-Workflow-Stock-B8646-Baseline' in source
    assert '--stock-b8646-serving' in source
    assert 'shared stock-ADK and llama.cpp b8646 compatibility baseline' in source
    assert "timeout(time: 70, unit: 'MINUTES')" in open(
        'jenkins/python/llm_agent_cd/evaluation_prepare.py').read()


def test_default_agent_prompt_and_a2a_deploy_has_a_distinct_locked_job():
    from inspect import signature
    from jenkins.python.llm_agent_cd import evaluation_prepare
    assert 'default_agents' in signature(evaluation_prepare.install).parameters
    source = open('jenkins/python/llm_agent_cd/evaluation_prepare.py').read()
    deploy = open('jenkins/python/llm_agent_cd/default_agents_deploy.py').read()
    assert 'RecSys-Workflow-Default-Agents-Prepare' in source
    assert 'llm_agent_cd.default_agents_deploy' in source
    assert "coordinator_direct_mcp_tools':0" in deploy
    assert "context_exact_null_single_call':True" in deploy


def test_stock_ab_capacity_window_is_lock_protected_and_fail_restores():
    from inspect import signature
    from pathlib import Path
    from jenkins.python.llm_agent_cd import evaluation_prepare

    assert 'stock_ab_capacity' in signature(evaluation_prepare.install).parameters
    installer = Path('jenkins/python/llm_agent_cd/evaluation_prepare.py').read_text()
    source = Path('jenkins/python/llm_agent_cd/stock_ab_capacity.py').read_text()
    assert 'RecSys-Workflow-Stock-AB-Capacity' in installer
    assert 'llm_agent_cd.stock_ab_capacity' in installer
    assert 'workflows.argoproj.io' in source
    assert 'driver.preflight' in source
    assert 'except Exception:' in source and '_restore(targets, original_pool_replicas)' in source
    assert 'kubeflow_storage_retained' in source


def test_idle_previous_adapter_capacity_is_locked_and_preserves_route():
    from inspect import signature
    from pathlib import Path
    from jenkins.python.llm_agent_cd import evaluation_prepare

    assert 'idle_previous_adapter_capacity' in signature(
        evaluation_prepare.install).parameters
    installer = Path('jenkins/python/llm_agent_cd/evaluation_prepare.py').read_text()
    source = Path(
        'jenkins/python/llm_agent_cd/idle_previous_adapter_capacity.py'
    ).read_text()
    assert 'RecSys-Workflow-Idle-Previous-Adapter-Capacity' in installer
    assert 'llm_agent_cd.idle_previous_adapter_capacity' in installer
    assert 'driver.verify_route(state, 0' in source
    assert 'IfNoneMatch="*"' in source
    assert 'driver.preflight(' in source
    assert 'restore_target(target_name, target_pool, release_id)' in source
    assert 'REVIEW_CPU = "reviewed-prompt-previous-cpu-adapter-v2"' in source
    assert 'config["target_role"] = "coordinator"' in source
    assert 'AB_ALLOW_LIVE_TEST="true"' in source
    assert 'retained N2 previous adapter has no Ready Service endpoint' in source
    assert 'inference_requests": 0' in source


def test_failed_candidate_loop_quarantine_is_exact_and_retains_backend():
    from inspect import signature
    from pathlib import Path
    from jenkins.python.llm_agent_cd import evaluation_prepare

    assert 'failed_candidate_loop_quarantine' in signature(
        evaluation_prepare.install).parameters
    installer = Path('jenkins/python/llm_agent_cd/evaluation_prepare.py').read_text()
    source = Path(
        'jenkins/python/llm_agent_cd/failed_candidate_loop_quarantine.py'
    ).read_text()
    assert 'RecSys-Workflow-Failed-Candidate-Loop-Quarantine' in installer
    assert 'llm_agent_cd.failed_candidate_loop_quarantine' in installer
    assert 'probe_evidence' in source and 'trajectory_before' in source
    assert '"delete",' in source and '"sandboxagent",' in source
    assert 'coordinator_modelconfig_retained' in source
    assert 'llm_backend_retained_ready' in source
    assert 'state_unchanged' in source and 'route_unchanged' in source


def test_failed_qwen25_backend_retirement_is_exact_and_reversible():
    from inspect import signature
    from pathlib import Path
    from jenkins.python.llm_agent_cd import evaluation_prepare

    assert 'retire_failed_qwen25_backend' in signature(
        evaluation_prepare.install).parameters
    installer = Path('jenkins/python/llm_agent_cd/evaluation_prepare.py').read_text()
    source = Path(
        'jenkins/python/llm_agent_cd/retire_failed_qwen25_backend.py'
    ).read_text()
    assert 'RecSys-Workflow-Retire-Failed-Qwen25-Backend' in installer
    assert 'llm_agent_cd.retire_failed_qwen25_backend' in installer
    assert 'failed Qwen2.5 backend is referenced by workflow state' in source
    assert 'failed release adapter is not quarantined' in source
    assert '"replace", "path": "/spec/replicas", "value": 0' in source
    assert 'immutable_resources_and_evidence_retained' in source
    assert 'state_unchanged' in source and 'route_unchanged' in source
    assert 'inference_requests": 0' in source


def test_terminal_candidate_capacity_is_exact_journaled_and_reversible():
    from inspect import signature
    from pathlib import Path
    from jenkins.python.llm_agent_cd import evaluation_prepare

    assert 'terminal_candidate_capacity' in signature(
        evaluation_prepare.install).parameters
    installer = Path(
        'jenkins/python/llm_agent_cd/evaluation_prepare.py').read_text()
    source = Path(
        'jenkins/python/llm_agent_cd/terminal_candidate_capacity_window.py'
    ).read_text()
    assert 'RecSys-Workflow-Terminal-Candidate-Capacity' in installer
    assert 'llm_agent_cd.terminal_candidate_capacity_window' in installer
    assert 'IfNoneMatch="*"' in source
    assert 'driver.preflight(' in source
    assert '_restore(actions)' in source
    assert 'ROLLBACK_FAILED' in source
    assert 'recsys-coordinator-sandbox-pool' in source
    assert 'langfuse-postgresql' in source
    assert 'keda-operator-metrics-apiserver' in source
    assert 'scaledobject' in source
    assert 'minReplicaCount' in source and 'maxReplicaCount' in source
    assert 'httpscaledobjects.http.keda.sh' in source
    assert 'KEDA HTTP add-on has a live HTTPScaledObject consumer' in source
    assert 'unused_keda_http_addons_paused' in source
    assert 'reviewed historical adapter gained a session' in source
    assert 'historical state release mapping mismatch' in source
    assert 'state_unchanged' in source and 'route_unchanged' in source
    assert 'inference_requests": 0' in source


def test_failed_terminal_candidate_quarantine_is_exact_and_no_new_inference():
    from inspect import signature
    from pathlib import Path
    from jenkins.python.llm_agent_cd import evaluation_prepare

    assert 'failed_terminal_candidate_quarantine' in signature(
        evaluation_prepare.install).parameters
    installer = Path(
        'jenkins/python/llm_agent_cd/evaluation_prepare.py').read_text()
    source = Path(
        'jenkins/python/llm_agent_cd/failed_terminal_candidate_quarantine.py'
    ).read_text()
    assert 'RecSys-Workflow-Failed-Terminal-Candidate-Quarantine' in installer
    assert 'llm_agent_cd.failed_terminal_candidate_quarantine' in installer
    assert 'd406faebb76eff219b3995107c6975e57ea337a9b60a709da4e40a4e842acc9e' in source
    assert 'timeout_or_transport_failure_no_retry' in source
    assert 'substrate worker pool has no free workers' in source
    assert 'IfNoneMatch="*"' in source
    assert 'coordinator_sandboxagent_removed' in source
    assert 'llm_backend_retained_ready' in source
    assert 'online_ab_requests": 0' in source
    assert 'inference_requests": 0' in source


def test_candidate_full_a2a_preflight_is_zero_weight_and_create_only():
    from inspect import signature
    from pathlib import Path
    from jenkins.python.llm_agent_cd import evaluation_prepare

    assert 'candidate_a2a_preflight' in signature(evaluation_prepare.install).parameters
    installer = Path('jenkins/python/llm_agent_cd/evaluation_prepare.py').read_text()
    source = Path('jenkins/python/llm_agent_cd/candidate_a2a_preflight.py').read_text()
    assert 'RecSys-Workflow-Candidate-A2A-Preflight' in installer
    assert 'llm_agent_cd.candidate_a2a_preflight' in installer
    assert 'readonly_candidate_a2a_probes' in source
    assert 'driver.verify_route(state, 0' in source
    assert 'driver.deploy(candidate)' in source
    assert 'inference_requests": 3' in source
    assert 'offline_requests": 0' in source
    assert 'synthetic_requests": 0' in source
    assert 'custom_guard' not in source


def test_failed_unrouted_candidate_capacity_requires_quiescent_task_evidence():
    from inspect import signature
    from pathlib import Path
    from jenkins.python.llm_agent_cd import evaluation_prepare

    assert 'failed_candidate_a2a_preflight_capacity' in signature(
        evaluation_prepare.install).parameters
    installer = Path('jenkins/python/llm_agent_cd/evaluation_prepare.py').read_text()
    source = Path(
        'jenkins/python/llm_agent_cd/failed_candidate_a2a_preflight_capacity.py'
    ).read_text()
    assert 'RecSys-Workflow-Failed-Candidate-A2A-Capacity' in installer
    assert 'first != second' in source
    assert 'first["calls"] != first["responses"]' in source
    assert 'sessions or invocations' in source
    assert 'driver.verify_route(state, 0' in source
    assert 'release_backend_agents_and_evidence_retained' in source
