"""Install the operator-reviewed, lock-protected, preparation-only Jenkins job."""
import base64
import hashlib
import io
from pathlib import Path
import tarfile

from .provision import secret, forward


def groovy_base64(data):
    # JVM string constants have a 64 KiB limit; never embed a whole archive
    # or Pipeline source in one constant.
    encoded = base64.b64encode(data).decode()
    return '[' + ','.join(repr(encoded[i:i+16000]) for i in range(0,len(encoded),16000)) + "].join('')"


def bundle(root=Path('.'), *, serving=False, default_agents=False,
           context_contract=False):
    paths = [root / path for path in (
        'apps/agentic/llm_ab_router', 'jenkins/python/llm_agent_cd', 'jenkins/python/model_cd',
        'jenkins/scripts/deploy/global_model_config_locked.sh',
        'jenkins/scripts/lib/common.sh',
        'configs/llm-ab', 'configs/agentic/recsys-context-agent/tools-contract.json',
        'configs/agentic/mcp-auth-versions.yaml',
        'infra/helm/recsys-workflow-ab', 'infra/helm/recsys-global-model-config',
        'infra/helm/recsys-kagent-agent', 'infra/helm/recsys-recommendation-agent',
        'infra/helm/recsys-coordinator-agent',
        'infra/helm/recsys-observability/dashboards/llm-ab-rollout.json')]
    output = io.BytesIO()
    if serving:
        paths.append(root/'infra/helm/recsys-online-feature-api')
    if context_contract:
        paths.extend((root/'infra/helm/recsys-online-feature-api',
                      root/'infra/helm/recsys-feature-rag-mcp'))
    if default_agents:
        paths.extend((root/'infra/helm/recsys-kagent-agent',
                      root/'infra/helm/recsys-coordinator-agent'))
    with tarfile.open(fileobj=output, mode='w:gz') as archive:
        for path in paths:
            files = sorted(path.rglob('*')) if path.is_dir() else [path]
            for item in files:
                if not item.is_file() or '__pycache__' in item.parts or item.suffix == '.pyc':
                    continue
                if item.is_symlink():
                    raise ValueError('preparation bundle cannot contain symlinks')
                archive.add(item, arcname=str(item.relative_to(root)), recursive=False)
    return output.getvalue()


def install(*, default_agents=False, baseline=False, verify_serving=False, cutover=False,
            trigger=False, enable_dispatch=False, maintain_terminal=False, data_recovery=False, revise_active=False,
            serving=False, capacity=False, active_capacity=False,
            adapter_placement_capacity=False, previous_placement_capacity=False,
            current_release_capacity=False,
            candidate_release_recovery=False, candidate_telemetry_recovery=False,
            candidate_fixture_recovery=False, candidate_structured_fixture_recovery=False,
            candidate_runtime_parser_recovery=False, candidate_offline_wiring_recovery=False,
            failed_prepared_baseline_capacity=False,
            failed_candidate_capacity=False, failed_baseline_capacity=False, previous_v4_capacity=False,
            stock_b8646_serving=False, stock_ab_capacity=False,
            candidate_a2a_preflight=False,
            failed_candidate_a2a_preflight_capacity=False,
            context_contract=False,
            sessionless_adapter_capacity=False, acceptance_capacity=False,
            idle_previous_adapter_capacity=False,
            prompt_ab_adapter_capacity=False,
            failed_candidate_loop_quarantine=False,
            retire_failed_qwen25_backend=False,
            terminal_candidate_capacity=False,
            failed_terminal_candidate_quarantine=False,
            coordinator_worker_capacity=False,
            coordinator_prompt_migration=False,
            coordinator_candidate_a2a_preflight=False,
            failed_prompt_candidate_quarantine=False,
            failed_coordinator_candidate_quarantine=False):
    import requests
    data = bundle(serving=serving,default_agents=default_agents,
                  context_contract=context_contract)
    checksum = hashlib.sha256(data).hexdigest()
    source = Path('jenkins/LLMWorkflowEvaluationPrepare.Jenkinsfile').read_text().replace(
        '__SOURCE_CHECKSUM__', checksum).replace('__BUNDLE__', groovy_base64(data))
    if maintain_terminal:
        source=source.replace('llm_agent_cd.evaluation_deploy --image','llm_agent_cd.evaluation_deploy --maintain-terminal --image')
    if default_agents:
        source=source.replace('llm_agent_cd.evaluation_deploy',
            'llm_agent_cd.default_agents_deploy').replace(
            'Capacity, Additive Schema and Suspended Evaluation',
            'Deploy Stock-ADK Context and A2A-only Coordinator Defaults')
    if baseline:
        source=source.replace('llm_agent_cd.evaluation_deploy','llm_agent_cd.baseline_prepare').replace(
            'Capacity, Additive Schema and Suspended Evaluation','Fresh Snapshot, Immutable Baseline and Readiness')
        if revise_active:
            source=source.replace('llm_agent_cd.baseline_prepare ', 'llm_agent_cd.baseline_prepare --revise-active ')
        if verify_serving:
            source=source.replace('llm_agent_cd.baseline_prepare ', 'llm_agent_cd.baseline_prepare --verify-serving ')
        if stock_b8646_serving:
            if not revise_active or not verify_serving:
                raise ValueError('stock b8646 baseline requires active revision and serving probes')
            source=source.replace('llm_agent_cd.baseline_prepare ',
                                  'llm_agent_cd.baseline_prepare --stock-b8646-serving ')
            # Six non-replayable probes each use the production 600-second
            # request budget. The outer lock must not kill the final composite.
            source=source.replace("timeout(time: 25, unit: 'MINUTES')",
                                  "timeout(time: 70, unit: 'MINUTES')")
        if coordinator_prompt_migration:
            if not revise_active or not verify_serving or stock_b8646_serving:
                raise ValueError('Coordinator prompt baseline requires active revision and three serving probes')
            source=source.replace('llm_agent_cd.baseline_prepare ',
                                  'llm_agent_cd.baseline_prepare --coordinator-prompt-migration ')
            source=source.replace("timeout(time: 25, unit: 'MINUTES')",
                                  "timeout(time: 45, unit: 'MINUTES')")
    if cutover:
        source=source.replace('llm_agent_cd.evaluation_deploy','llm_agent_cd.baseline_cutover').replace(
            'Capacity, Additive Schema and Suspended Evaluation','Commit Prepared Baseline through Verified Routing')
    if trigger:
        source=source.replace('llm_agent_cd.evaluation_deploy','llm_agent_cd.trigger_deploy').replace(
            'Capacity, Additive Schema and Suspended Evaluation','Scoped Webhook and Dispatch Readiness')
        if enable_dispatch:
            source=source.replace('llm_agent_cd.trigger_deploy --image','llm_agent_cd.trigger_deploy --enable-dispatch --image')
    if data_recovery:
        source=source.replace('llm_agent_cd.evaluation_deploy','llm_agent_cd.candidate_recovery').replace(
            'Capacity, Additive Schema and Suspended Evaluation','Inspect or Restore Source-backed Candidate Index')
    if serving:
        source=source.replace('llm_agent_cd.evaluation_deploy','llm_agent_cd.serving_recovery_deploy').replace(
            'Capacity, Additive Schema and Suspended Evaluation','Verify Image-only Serving Repair and Capacity')
    if capacity:
        source=source.replace('llm_agent_cd.evaluation_deploy','llm_agent_cd.capacity_window').replace(
            'Capacity, Additive Schema and Suspended Evaluation','Journal Retired Adapters and Unchanged Probe Placement')
    if active_capacity:
        source=source.replace('llm_agent_cd.evaluation_deploy','llm_agent_cd.active_capacity_window').replace(
            'Capacity, Additive Schema and Suspended Evaluation','Retain Historical Releases at One Adapter Replica')
    if adapter_placement_capacity:
        source=source.replace('llm_agent_cd.evaluation_deploy','llm_agent_cd.adapter_placement_capacity').replace(
            'Capacity, Additive Schema and Suspended Evaluation','Retain Disabled Candidate on One Adapter Placement')
    if previous_placement_capacity:
        source=source.replace('llm_agent_cd.evaluation_deploy','llm_agent_cd.previous_adapter_capacity').replace(
            'Capacity, Additive Schema and Suspended Evaluation','Retain Previous Champion on One Adapter Placement')
    if current_release_capacity:
        source=source.replace('llm_agent_cd.evaluation_deploy','llm_agent_cd.current_release_capacity').replace(
            'Capacity, Additive Schema and Suspended Evaluation','Retain Current Releases on N2 Adapter Placement')
    if stock_ab_capacity:
        source=source.replace('llm_agent_cd.evaluation_deploy','llm_agent_cd.stock_ab_capacity').replace(
            'Capacity, Additive Schema and Suspended Evaluation','Journal Stock-ADK LLM-only Acceptance Capacity')
    if candidate_a2a_preflight:
        source=source.replace('llm_agent_cd.evaluation_deploy','llm_agent_cd.candidate_a2a_preflight').replace(
            'Capacity, Additive Schema and Suspended Evaluation','Deploy and Probe Unrouted Candidate Full A2A')
        source=source.replace("timeout(time: 25, unit: 'MINUTES')",
                              "timeout(time: 45, unit: 'MINUTES')")
    if failed_candidate_a2a_preflight_capacity:
        source=source.replace('llm_agent_cd.evaluation_deploy',
                              'llm_agent_cd.failed_candidate_a2a_preflight_capacity').replace(
            'Capacity, Additive Schema and Suspended Evaluation',
            'Release Failed Unrouted Candidate A2A Adapters')
    if failed_candidate_capacity:
        source=source.replace('llm_agent_cd.evaluation_deploy','llm_agent_cd.failed_candidate_capacity').replace(
            'Capacity, Additive Schema and Suspended Evaluation','Release Rolled-back Candidate Adapter Capacity')
    if failed_baseline_capacity:
        source=source.replace('llm_agent_cd.evaluation_deploy','llm_agent_cd.failed_baseline_capacity').replace(
            'Capacity, Additive Schema and Suspended Evaluation','Release Failed Baseline Adapter Capacity')
    if previous_v4_capacity:
        source=source.replace('llm_agent_cd.evaluation_deploy','llm_agent_cd.previous_v4_capacity').replace(
            'Capacity, Additive Schema and Suspended Evaluation','Retain Previous v4 on One Adapter Placement')
    if candidate_release_recovery:
        source=source.replace('llm_agent_cd.evaluation_deploy','llm_agent_cd.candidate_release_recovery').replace(
            'Capacity, Additive Schema and Suspended Evaluation','Recover Pre-inference Candidate after Attestation Fix')
    if candidate_telemetry_recovery:
        source=source.replace('llm_agent_cd.evaluation_deploy','llm_agent_cd.candidate_telemetry_recovery').replace(
            'Capacity, Additive Schema and Suspended Evaluation','Recover Candidate after Router Telemetry Fix')
    if candidate_fixture_recovery:
        source=source.replace('llm_agent_cd.evaluation_deploy','llm_agent_cd.candidate_fixture_recovery').replace(
            'Capacity, Additive Schema and Suspended Evaluation','Recover Candidate after Explicit-null Fixture Revision')
    if candidate_structured_fixture_recovery:
        source=source.replace('llm_agent_cd.evaluation_deploy','llm_agent_cd.candidate_structured_fixture_recovery').replace(
            'Capacity, Additive Schema and Suspended Evaluation','Recover Candidate after Structured-request Fixture Revision')
    if candidate_runtime_parser_recovery:
        source=source.replace('llm_agent_cd.evaluation_deploy','llm_agent_cd.candidate_runtime_parser_recovery').replace(
            'Capacity, Additive Schema and Suspended Evaluation','Recover Candidate after Runtime-aware Parser Rollout')
    if candidate_offline_wiring_recovery:
        source=source.replace('llm_agent_cd.evaluation_deploy','llm_agent_cd.candidate_offline_wiring_recovery').replace(
            'Capacity, Additive Schema and Suspended Evaluation','Recover Offline-only Candidate after Frozen-suite Fix')
    if failed_prepared_baseline_capacity:
        source=source.replace('llm_agent_cd.evaluation_deploy','llm_agent_cd.failed_prepared_baseline_capacity').replace(
            'Capacity, Additive Schema and Suspended Evaluation','Release Failed Never-activated Baseline Adapters')
    if context_contract:
        source=source.replace('llm_agent_cd.evaluation_deploy','llm_agent_cd.context_contract_deploy').replace(
            'Capacity, Additive Schema and Suspended Evaluation','Deploy Nullable Context Contract v2')
    if sessionless_adapter_capacity:
        source=source.replace('llm_agent_cd.evaluation_deploy','llm_agent_cd.sessionless_adapter_capacity').replace(
            'Capacity, Additive Schema and Suspended Evaluation','Release Sessionless Historical Adapter Capacity')
    if acceptance_capacity:
        source=source.replace('llm_agent_cd.evaluation_deploy','llm_agent_cd.acceptance_capacity').replace(
            'Capacity, Additive Schema and Suspended Evaluation','Reserve Stock A/B Acceptance Capacity')
    if idle_previous_adapter_capacity:
        source=source.replace('llm_agent_cd.evaluation_deploy',
            'llm_agent_cd.idle_previous_adapter_capacity').replace(
            'Capacity, Additive Schema and Suspended Evaluation',
            'Release Duplicate Idle Previous Adapter and Verify Candidate Capacity')
    if prompt_ab_adapter_capacity:
        source=source.replace('llm_agent_cd.evaluation_deploy',
            'llm_agent_cd.prompt_ab_adapter_capacity').replace(
            'Capacity, Additive Schema and Suspended Evaluation',
            'Exchange Cross-node Duplicate Adapters and Verify Prompt A/B Capacity')
    if failed_candidate_loop_quarantine:
        source=source.replace('llm_agent_cd.evaluation_deploy',
            'llm_agent_cd.failed_candidate_loop_quarantine').replace(
            'Capacity, Additive Schema and Suspended Evaluation',
            'Quarantine Never-routed Candidate Coordinator Loop')
    if retire_failed_qwen25_backend:
        source=source.replace('llm_agent_cd.evaluation_deploy',
            'llm_agent_cd.retire_failed_qwen25_backend').replace(
            'Capacity, Additive Schema and Suspended Evaluation',
            'Retire Quarantined Qwen2.5 Backend Capacity')
    if terminal_candidate_capacity:
        source=source.replace('llm_agent_cd.evaluation_deploy',
            'llm_agent_cd.terminal_candidate_capacity_window').replace(
            'Capacity, Additive Schema and Suspended Evaluation',
            'Journal and Verify Terminal Candidate Capacity')
        source=source.replace("timeout(time: 25, unit: 'MINUTES')",
                              "timeout(time: 45, unit: 'MINUTES')")
    if failed_terminal_candidate_quarantine:
        source=source.replace('llm_agent_cd.evaluation_deploy',
            'llm_agent_cd.failed_terminal_candidate_quarantine').replace(
            'Capacity, Additive Schema and Suspended Evaluation',
            'Quarantine Failed Terminal Candidate')
    if coordinator_worker_capacity:
        source=source.replace('llm_agent_cd.evaluation_deploy',
            'llm_agent_cd.coordinator_worker_capacity').replace(
            'Capacity, Additive Schema and Suspended Evaluation',
            'Reserve Two Coordinator Workers for Coordinator-only A/B')
        source=source.replace("timeout(time: 25, unit: 'MINUTES')",
                              "timeout(time: 45, unit: 'MINUTES')")
    if coordinator_candidate_a2a_preflight:
        source=source.replace('llm_agent_cd.evaluation_deploy',
            'llm_agent_cd.coordinator_candidate_a2a_preflight').replace(
            'Capacity, Additive Schema and Suspended Evaluation',
            'Deploy and Probe Unrouted Coordinator-only Candidate')
        source=source.replace("timeout(time: 25, unit: 'MINUTES')",
                              "timeout(time: 45, unit: 'MINUTES')")
    if failed_coordinator_candidate_quarantine:
        source=source.replace('llm_agent_cd.evaluation_deploy',
            'llm_agent_cd.failed_coordinator_candidate_quarantine').replace(
            'Capacity, Additive Schema and Suspended Evaluation',
            'Quarantine Failed Coordinator-only Candidate')
    if failed_prompt_candidate_quarantine:
        source=source.replace('llm_agent_cd.evaluation_deploy',
            'llm_agent_cd.failed_prompt_candidate_quarantine').replace(
            'Capacity, Additive Schema and Suspended Evaluation',
            'Quarantine Failed Frozen-prompt Coordinator Candidate')
    script = '''
import jenkins.model.Jenkins
import org.jenkinsci.plugins.workflow.job.WorkflowJob
import org.jenkinsci.plugins.workflow.cps.CpsFlowDefinition
import hudson.model.ParametersDefinitionProperty
import hudson.model.StringParameterDefinition
def j = Jenkins.get()
def job = j.getItem('RecSys-Workflow-Evaluation-Prepare')
if (job != null && (!job.description?.startsWith('Workflow evaluation preparation:') || job.isBuilding() || job.isInQueue())) {
  throw new IllegalStateException('Refusing foreign/running/queued preparation job')
}
if (job == null) job = j.createProject(WorkflowJob, 'RecSys-Workflow-Evaluation-Prepare')
job.description = 'Workflow evaluation preparation: additive deploy only; dispatch disabled; no route/champion change. Source __CHECKSUM__'
job.definition = new CpsFlowDefinition(new String((__SOURCE__).decodeBase64(), 'UTF-8'), true)
job.addProperty(new ParametersDefinitionProperty([
  new StringParameterDefinition('IMAGE', '', 'Pinned operator-owned evaluator/router image')
]))
job.save()
println('EVALUATION_PREPARATION_READY')
'''.replace('__SOURCE__', groovy_base64(source.encode())).replace('__CHECKSUM__', checksum)
    if default_agents:
        script=script.replace('RecSys-Workflow-Evaluation-Prepare',
            'RecSys-Workflow-Default-Agents-Prepare').replace(
            'Workflow evaluation preparation:','Workflow default-agent preparation:').replace(
            'additive deploy only; dispatch disabled; no route/champion change.',
            'Context exact-null prompt and Coordinator A2A-only tools; preserve global config, route and champion.')
    if baseline:
        script=script.replace('RecSys-Workflow-Evaluation-Prepare','RecSys-Workflow-Baseline-Prepare').replace(
            'Workflow evaluation preparation:','Workflow baseline preparation:')
    if stock_b8646_serving:
        script=script.replace('RecSys-Workflow-Baseline-Prepare',
                              'RecSys-Workflow-Stock-B8646-Baseline').replace(
            'Workflow baseline preparation:', 'Workflow stock b8646 baseline:').replace(
            'additive deploy only; dispatch disabled; no route/champion change.',
            'shared stock-ADK and llama.cpp b8646 compatibility baseline; activation requires a separate verified cutover.')
    if coordinator_prompt_migration:
        script=script.replace('RecSys-Workflow-Baseline-Prepare',
                              'RecSys-Workflow-Coordinator-Prompt-Baseline').replace(
            'Workflow baseline preparation:', 'Workflow Coordinator prompt baseline:').replace(
            'additive deploy only; dispatch disabled; no route/champion change.',
            'native sequential immutable baseline with per-call isolated child sessions; exactly three create-only Coordinator flows; activation requires separate cutover.')
    if cutover:
        script=script.replace('RecSys-Workflow-Evaluation-Prepare','RecSys-Workflow-Baseline-Cutover').replace(
            'Workflow evaluation preparation:','Workflow baseline cutover:')
        script=script.replace('additive deploy only; dispatch disabled; no route/champion change.',
            'explicit prepared-baseline intent, Envoy verification and CAS activation; no experiment inference.')
    if trigger:
        script=script.replace('RecSys-Workflow-Evaluation-Prepare','RecSys-Workflow-Dispatch-Prepare').replace(
            'Workflow evaluation preparation:','Workflow dispatch preparation:')
    if maintain_terminal:
        script=script.replace('additive deploy only; dispatch disabled; no route/champion change.',
            'terminal tooling maintenance; preserve dispatch settings and route/champion; no inference.')
    if data_recovery:
        script=script.replace('RecSys-Workflow-Evaluation-Prepare','RecSys-Workflow-Candidate-Recovery').replace(
            'Workflow evaluation preparation:','Workflow candidate recovery:').replace(
            'additive deploy only; dispatch disabled; no route/champion change.',
            'create-only global candidate index from verified real feature rows; no inference or route/champion change.')
    if serving:
        script=script.replace('RecSys-Workflow-Evaluation-Prepare','RecSys-Workflow-Serving-Repair').replace(
            'Workflow evaluation preparation:','Workflow serving repair:').replace(
            'additive deploy only; dispatch disabled; no route/champion change.',
            'image-only feature API repair; capacity and Helm diff guarded; no inference or route/champion change.')
    if capacity or active_capacity:
        script=script.replace('RecSys-Workflow-Evaluation-Prepare','RecSys-Workflow-Capacity-Prepare').replace(
            'Workflow evaluation preparation:','Workflow capacity preparation:').replace(
            'additive deploy only; dispatch disabled; no route/champion change.',
            'journaled retirement of unused/quarantined adapters and probe placement only; keep telemetry and resource budgets.')
    if adapter_placement_capacity:
        script=script.replace('RecSys-Workflow-Evaluation-Prepare','RecSys-Workflow-Adapter-Placement-Capacity').replace(
            'Workflow evaluation preparation:','Workflow adapter placement capacity:').replace(
            'additive deploy only; dispatch disabled; no route/champion change.',
            'retain disabled candidate on one Ready placement; release duplicate adapter request only; no inference or route/champion change.')
    if previous_placement_capacity:
        script=script.replace('RecSys-Workflow-Evaluation-Prepare','RecSys-Workflow-Previous-Placement-Capacity').replace(
            'Workflow evaluation preparation:','Workflow previous placement capacity:').replace(
            'additive deploy only; dispatch disabled; no route/champion change.',
            'retain previous champion on one Ready placement; release duplicate adapter request only; no inference or route/champion change.')
    if current_release_capacity:
        script=script.replace('RecSys-Workflow-Evaluation-Prepare','RecSys-Workflow-Current-Release-Capacity').replace(
            'Workflow evaluation preparation:','Workflow current-release capacity:').replace(
            'additive deploy only; dispatch disabled; no route/champion change.',
            'journaled single-placement change for current champion and rolled-back candidate; peers, sessions, route and state retained; no inference.')
    if stock_ab_capacity:
        script=script.replace('RecSys-Workflow-Evaluation-Prepare','RecSys-Workflow-Stock-AB-Capacity').replace(
            'Workflow evaluation preparation:','Workflow stock A/B capacity:').replace(
            'additive deploy only; dispatch disabled; no route/champion change.',
            'journaled idle Kubeflow Pipelines pause and one legacy Recommendation router worker; exact candidate preflight; no inference.')
    if candidate_a2a_preflight:
        script=script.replace('RecSys-Workflow-Evaluation-Prepare','RecSys-Workflow-Candidate-A2A-Preflight').replace(
            'Workflow evaluation preparation:','Workflow candidate full-A2A preflight:').replace(
            'additive deploy only; dispatch disabled; no route/champion change.',
            'deploy exact Qwen2.5 candidate at zero weight and run three create-only Coordinator A2A probes; no experiment or route change.')
    if failed_candidate_a2a_preflight_capacity:
        script=script.replace('RecSys-Workflow-Evaluation-Prepare',
            'RecSys-Workflow-Failed-Candidate-A2A-Capacity').replace(
            'Workflow evaluation preparation:',
            'Workflow failed candidate A2A capacity:').replace(
            'additive deploy only; dispatch disabled; no route/champion change.',
            'scale only the adapters of exact failed unrouted candidate after quiescence proof; retain backend, agents and evidence.')
    if failed_candidate_capacity:
        script=script.replace('RecSys-Workflow-Evaluation-Prepare','RecSys-Workflow-Failed-Candidate-Capacity').replace(
            'Workflow evaluation preparation:','Workflow failed candidate capacity:').replace(
            'additive deploy only; dispatch disabled; no route/champion change.',
            'journaled scale-to-zero of the current disabled candidate with no sessions; retain release/backend/evidence; no inference.')
    if failed_baseline_capacity:
        script=script.replace('RecSys-Workflow-Evaluation-Prepare','RecSys-Workflow-Failed-Baseline-Capacity').replace(
            'Workflow evaluation preparation:','Workflow failed baseline capacity:').replace(
            'additive deploy only; dispatch disabled; no route/champion change.',
            'journaled scale-to-zero of an exact failed unactivated baseline; retain release/backend/evidence; no inference.')
    if previous_v4_capacity:
        script=script.replace('RecSys-Workflow-Evaluation-Prepare','RecSys-Workflow-Previous-v4-Capacity').replace(
            'Workflow evaluation preparation:','Workflow previous v4 capacity:').replace(
            'additive deploy only; dispatch disabled; no route/champion change.',
            'retain previous workflow on one Ready adapter placement; preserve sessions/release/backend; no inference.')
    if candidate_release_recovery:
        script=script.replace('RecSys-Workflow-Evaluation-Prepare','RecSys-Workflow-Candidate-Release-Recovery').replace(
            'Workflow evaluation preparation:','Workflow candidate release recovery:').replace(
            'additive deploy only; dispatch disabled; no route/champion change.',
            'pre-inference attestation recovery only; immutable release/route/champion verified; no inference.')
    if candidate_telemetry_recovery:
        script=script.replace('RecSys-Workflow-Evaluation-Prepare','RecSys-Workflow-Candidate-Telemetry-Recovery').replace(
            'Workflow evaluation preparation:','Workflow candidate telemetry recovery:').replace(
            'additive deploy only; dispatch disabled; no route/champion change.',
            'telemetry-only recovery after synced offline PASS; immutable release/route/champion verified; no inference.')
    if candidate_fixture_recovery:
        script=script.replace('RecSys-Workflow-Evaluation-Prepare','RecSys-Workflow-Candidate-Fixture-Recovery').replace(
            'Workflow evaluation preparation:','Workflow candidate fixture recovery:').replace(
            'additive deploy only; dispatch disabled; no route/champion change.',
            'candidate did not fail; versioned fixture ambiguity recovery only; route/champion verified; no inference.')
    if candidate_structured_fixture_recovery:
        script=script.replace('RecSys-Workflow-Evaluation-Prepare','RecSys-Workflow-Candidate-Structured-Fixture-Recovery').replace(
            'Workflow evaluation preparation:','Workflow candidate structured-fixture recovery:').replace(
            'additive deploy only; dispatch disabled; no route/champion change.',
            'candidate received no online call; structured request fixture recovery only; route/champion verified; no inference.')
    if candidate_runtime_parser_recovery:
        script=script.replace('RecSys-Workflow-Evaluation-Prepare','RecSys-Workflow-Candidate-Runtime-Parser-Recovery').replace(
            'Workflow evaluation preparation:','Workflow candidate runtime-parser recovery:').replace(
            'additive deploy only; dispatch disabled; no route/champion change.',
            'exact pre-v4 parser telemetry recovery; candidate received no online/synthetic call; no inference or route/champion change.')
    if candidate_offline_wiring_recovery:
        script=script.replace('RecSys-Workflow-Evaluation-Prepare','RecSys-Workflow-Candidate-Offline-Wiring-Recovery').replace(
            'Workflow evaluation preparation:','Workflow candidate offline-wiring recovery:').replace(
            'additive deploy only; dispatch disabled; no route/champion change.',
            'exact six-PASS offline-only recovery after frozen-suite evaluator fix; no inference or route change.')
    if failed_prepared_baseline_capacity:
        script=script.replace('RecSys-Workflow-Evaluation-Prepare','RecSys-Workflow-Failed-Prepared-Baseline-Capacity').replace(
            'Workflow evaluation preparation:','Workflow failed prepared-baseline capacity:').replace(
            'additive deploy only; dispatch disabled; no route/champion change.',
            'journaled adapter scale-down for one exact failed, never-activated preparation; release/backend/evidence retained; no inference.')
    if context_contract:
        script=script.replace('RecSys-Workflow-Evaluation-Prepare','RecSys-Workflow-Context-Contract-v2').replace(
            'Workflow evaluation preparation:','Workflow Context contract v2:').replace(
            'additive deploy only; dispatch disabled; no route/champion change.',
            'locked image/schema deploy for explicit nullable candidate semantics; no inference or route/champion change.')
    if sessionless_adapter_capacity:
        script=script.replace('RecSys-Workflow-Evaluation-Prepare','RecSys-Workflow-Sessionless-History-Capacity').replace(
            'Workflow evaluation preparation:','Workflow sessionless-history capacity:').replace(
            'additive deploy only; dispatch disabled; no route/champion change.',
            'journaled scale-to-zero of one exact unrouted release with zero sessions; release/backend/evidence retained; no inference.')
    if acceptance_capacity:
        script=script.replace('RecSys-Workflow-Evaluation-Prepare','RecSys-Workflow-Stock-Acceptance-Capacity').replace(
            'Workflow evaluation preparation:','Workflow stock acceptance capacity:').replace(
            'additive deploy only; dispatch disabled; no route/champion change.',
            'journaled exact adapter scale-down with Ready peers for serving pointers and zero-session history; no inference.')
    if idle_previous_adapter_capacity:
        script=script.replace('RecSys-Workflow-Evaluation-Prepare',
            'RecSys-Workflow-Idle-Previous-Adapter-Capacity').replace(
            'Workflow evaluation preparation:',
            'Workflow idle previous adapter capacity:').replace(
            'additive deploy only; dispatch disabled; no route/champion change.',
            'journaled E2 duplicate previous-adapter scale-down; retain its Ready N2 endpoint and verify exact candidate capacity; no inference.')
    if prompt_ab_adapter_capacity:
        script=script.replace('RecSys-Workflow-Evaluation-Prepare',
            'RecSys-Workflow-Prompt-AB-Adapter-Capacity').replace(
            'Workflow evaluation preparation:',
            'Workflow prompt A/B adapter capacity:').replace(
            'additive deploy only; dispatch disabled; no route/champion change.',
            'journaled cross-node duplicate-adapter exchange; retain one Ready endpoint per serving release; auto-restore on failed exact candidate preflight; no inference.')
    if failed_candidate_loop_quarantine:
        script=script.replace('RecSys-Workflow-Evaluation-Prepare',
            'RecSys-Workflow-Failed-Candidate-Loop-Quarantine').replace(
            'Workflow evaluation preparation:',
            'Workflow failed candidate loop quarantine:').replace(
            'additive deploy only; dispatch disabled; no route/champion change.',
            'snapshot exact never-routed failed candidate, stop its Coordinator actor, retain backend/specialists/evidence; no inference.')
    if retire_failed_qwen25_backend:
        script=script.replace('RecSys-Workflow-Evaluation-Prepare',
            'RecSys-Workflow-Retire-Failed-Qwen25-Backend').replace(
            'Workflow evaluation preparation:',
            'Workflow retired failed Qwen2.5 backend:').replace(
            'additive deploy only; dispatch disabled; no route/champion change.',
            'scale exact unused Qwen2.5 v2 backend to zero; retain immutable resources and evidence; no inference.')
    if terminal_candidate_capacity:
        script=script.replace('RecSys-Workflow-Evaluation-Prepare',
            'RecSys-Workflow-Terminal-Candidate-Capacity').replace(
            'Workflow evaluation preparation:',
            'Workflow terminal candidate capacity:').replace(
            'additive deploy only; dispatch disabled; no route/champion change.',
            'journaled exact CPU-request and replica exchange with automatic restore on failure; preserve state/route; no inference.')
    if failed_terminal_candidate_quarantine:
        script=script.replace('RecSys-Workflow-Evaluation-Prepare',
            'RecSys-Workflow-Failed-Terminal-Candidate-Quarantine').replace(
            'Workflow evaluation preparation:',
            'Workflow failed terminal candidate quarantine:').replace(
            'additive deploy only; dispatch disabled; no route/champion change.',
            'quarantine exact never-routed Qwen2.5 candidate after three create-only failures; retain backend and evidence; no new inference.')
    if coordinator_worker_capacity:
        script=script.replace('RecSys-Workflow-Evaluation-Prepare',
            'RecSys-Workflow-Coordinator-Worker-Capacity').replace(
            'Workflow evaluation preparation:',
            'Workflow Coordinator worker capacity:').replace(
            'additive deploy only; dispatch disabled; no route/champion change.',
            'journaled Coordinator-only scale from one to two workers; exact candidate capacity preflight; no inference or route/champion change.')
    if coordinator_candidate_a2a_preflight:
        script=script.replace('RecSys-Workflow-Evaluation-Prepare',
            'RecSys-Workflow-Coordinator-Candidate-Preflight').replace(
            'Workflow evaluation preparation:',
            'Workflow Coordinator-only candidate preflight:').replace(
            'additive deploy only; dispatch disabled; no route/champion change.',
            'deploy Coordinator-only Qwen2.5 candidate at zero weight and run exactly three create-only A2A probes; specialists remain on baseline LLM.')
    if failed_coordinator_candidate_quarantine:
        script=script.replace('RecSys-Workflow-Evaluation-Prepare',
            'RecSys-Workflow-Failed-Coordinator-Candidate-Quarantine').replace(
            'Workflow evaluation preparation:',
            'Workflow failed Coordinator-only candidate quarantine:').replace(
            'additive deploy only; dispatch disabled; no route/champion change.',
            'quarantine exact never-routed Coordinator-only candidate after two timeouts; retain backend, specialists, TaskStore and evidence; no new inference.')
    if failed_prompt_candidate_quarantine:
        script=script.replace('RecSys-Workflow-Evaluation-Prepare',
            'RecSys-Workflow-Failed-Prompt-Candidate-Quarantine').replace(
            'Workflow evaluation preparation:',
            'Workflow failed frozen-prompt candidate quarantine:').replace(
            'additive deploy only; dispatch disabled; no route/champion change.',
            'quarantine exact never-routed v30 candidate after three create-only failures; retain backend, specialists and immutable evidence; no new inference.')
    admin = secret('ci', 'recsys-jenkins-admin')
    with forward('ci', 'recsys-jenkins', 8080) as endpoint:
        client = requests.Session()
        client.auth = (admin['username'], admin['password'])
        crumb = client.get(endpoint + '/crumbIssuer/api/json', timeout=15)
        crumb.raise_for_status()
        client.headers[crumb.json()['crumbRequestField']] = crumb.json()['crumb']
        response = client.post(endpoint + '/scriptText', data={'script': script}, timeout=30)
        response.raise_for_status()
        if 'EVALUATION_PREPARATION_READY' not in response.text:
            raise RuntimeError('Jenkins installation rejected; response withheld')
    job_name = ('RecSys-Workflow-Coordinator-Prompt-Baseline' if coordinator_prompt_migration
                else 'RecSys-Workflow-Stock-B8646-Baseline' if stock_b8646_serving
                else 'RecSys-Workflow-Default-Agents-Prepare' if default_agents
                else 'RecSys-Workflow-Baseline-Prepare' if baseline
                else 'RecSys-Workflow-Baseline-Cutover' if cutover
                else 'RecSys-Workflow-Dispatch-Prepare' if trigger
                else 'RecSys-Workflow-Candidate-Release-Recovery' if candidate_release_recovery
                else 'RecSys-Workflow-Candidate-Telemetry-Recovery' if candidate_telemetry_recovery
                else 'RecSys-Workflow-Candidate-Fixture-Recovery' if candidate_fixture_recovery
                else 'RecSys-Workflow-Candidate-Structured-Fixture-Recovery' if candidate_structured_fixture_recovery
                else 'RecSys-Workflow-Candidate-Runtime-Parser-Recovery' if candidate_runtime_parser_recovery
                else 'RecSys-Workflow-Candidate-Offline-Wiring-Recovery' if candidate_offline_wiring_recovery
                else 'RecSys-Workflow-Context-Contract-v2' if context_contract
                else 'RecSys-Workflow-Sessionless-History-Capacity' if sessionless_adapter_capacity
                else 'RecSys-Workflow-Stock-Acceptance-Capacity' if acceptance_capacity
                else 'RecSys-Workflow-Idle-Previous-Adapter-Capacity' if idle_previous_adapter_capacity
                else 'RecSys-Workflow-Prompt-AB-Adapter-Capacity' if prompt_ab_adapter_capacity
                else 'RecSys-Workflow-Failed-Candidate-Loop-Quarantine' if failed_candidate_loop_quarantine
                else 'RecSys-Workflow-Retire-Failed-Qwen25-Backend' if retire_failed_qwen25_backend
                else 'RecSys-Workflow-Terminal-Candidate-Capacity' if terminal_candidate_capacity
                else 'RecSys-Workflow-Failed-Terminal-Candidate-Quarantine' if failed_terminal_candidate_quarantine
                else 'RecSys-Workflow-Coordinator-Worker-Capacity' if coordinator_worker_capacity
                else 'RecSys-Workflow-Coordinator-Candidate-Preflight' if coordinator_candidate_a2a_preflight
                else 'RecSys-Workflow-Failed-Prompt-Candidate-Quarantine' if failed_prompt_candidate_quarantine
                else 'RecSys-Workflow-Failed-Coordinator-Candidate-Quarantine' if failed_coordinator_candidate_quarantine
                else 'RecSys-Workflow-Failed-Prepared-Baseline-Capacity' if failed_prepared_baseline_capacity
                else 'RecSys-Workflow-Failed-Candidate-Capacity' if failed_candidate_capacity
                else 'RecSys-Workflow-Failed-Baseline-Capacity' if failed_baseline_capacity
                else 'RecSys-Workflow-Previous-v4-Capacity' if previous_v4_capacity
                else 'RecSys-Workflow-Current-Release-Capacity' if current_release_capacity
                else 'RecSys-Workflow-Stock-AB-Capacity' if stock_ab_capacity
                else 'RecSys-Workflow-Candidate-A2A-Preflight' if candidate_a2a_preflight
                else 'RecSys-Workflow-Failed-Candidate-A2A-Capacity' if failed_candidate_a2a_preflight_capacity
                else 'RecSys-Workflow-Adapter-Placement-Capacity' if adapter_placement_capacity
                else 'RecSys-Workflow-Previous-Placement-Capacity' if previous_placement_capacity
                else 'RecSys-Workflow-Evaluation-Prepare')
    print(job_name + ' installed; source checksum ' + checksum)


if __name__ == '__main__':
    install()
