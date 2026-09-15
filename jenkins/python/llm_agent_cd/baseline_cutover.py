"""Explicit inactive-bootstrap migration, with an intent before routing.

This does not overwrite an old immutable release or run an experiment. A lost
response can only resume the same intent; the public workflow is closed while
the intent is unfinished. Run inside the shared Jenkins release/state locks.
"""
from copy import deepcopy
import argparse
import json
import os
from pathlib import Path
import re
import time
from .driver import Driver, command
from .release import digest, release
from .release_guard import check
from .state import StateStore


ACTIVE_REVISION_KINDS = {
    'shared_stock_adk_b8646_a2a_prompt_revision_not_ab_experiment',
    'shared_stock_adk_b8646_a2a_runtime_render_revision_not_ab_experiment',
    'coordinator_terminal_prompt_v30_not_ab_experiment',
    'coordinator_native_isolated_sequential_prompt_v32_not_ab_experiment',
}


def validate_active_revision(prepared, state, baseline):
    """Reproduce an allowlisted migration instead of trusting its label."""
    kind = prepared.get('migration_kind')
    if kind not in ACTIVE_REVISION_KINDS:
        raise ValueError('unreviewed active baseline migration')
    if kind == 'coordinator_terminal_prompt_v30_not_ab_experiment':
        from .workflow_contract import revise_coordinator_terminal_prompt
        expected, audit = revise_coordinator_terminal_prompt(state['champion'])
    elif kind == 'coordinator_native_isolated_sequential_prompt_v32_not_ab_experiment':
        from .workflow_contract import revise_coordinator_native_sequential_baseline
        expected, audit = revise_coordinator_native_sequential_baseline(state['champion'])
    else:
        from .workflow_contract import revise_stock_runtime_and_b8646_serving
        runtime_image = prepared.get('audit', {}).get('expected_stock_runtime_image')
        adapter_image = prepared.get('audit', {}).get('expected_adapter_image')
        expected, audit = revise_stock_runtime_and_b8646_serving(
            state['champion'], baseline['llm'], runtime_image, adapter_image)
    if expected != baseline or prepared.get('audit') != audit:
        raise ValueError('active baseline migration evidence mismatch')


def check_stock_probes(store, baseline, migration_kind=None):
    from botocore.exceptions import ClientError
    if migration_kind == 'coordinator_native_isolated_sequential_prompt_v32_not_ab_experiment':
        probes = {
            'coordinator-recommendation': 'prompt-baseline-coordinator-recommendation-a2a-v33',
            'coordinator-context': 'prompt-baseline-coordinator-context-a2a-v33',
            'coordinator-composite': 'prompt-baseline-coordinator-composite-a2a-v33',
        }
    elif migration_kind == 'coordinator_terminal_prompt_v30_not_ab_experiment':
        probes = {
            'coordinator-recommendation': 'prompt-baseline-coordinator-recommendation-a2a-v30',
            'coordinator-context': 'prompt-baseline-coordinator-context-a2a-v30',
            'coordinator-composite': 'prompt-baseline-coordinator-composite-a2a-v30',
        }
    else:
        probes = {
            'recommendation': 'stock-recommendation-v29',
            'context': 'stock-context-exact-null-v29',
            'context-chunk': 'stock-context-exact-chunk-v29',
            'coordinator': 'stock-coordinator-recommendation-a2a-owner-v29',
            'coordinator-context': 'stock-coordinator-context-a2a-owner-v29',
            'coordinator-composite': 'stock-coordinator-composite-a2a-owner-v29',
        }
    for label, probe_id in probes.items():
        role = ('coordinator' if label.startswith('coordinator') else
                'context' if label.startswith('context') else label)
        root = 'workflow/runtime-preflights/' + baseline['release_id'] + '/' + probe_id
        try:
            result = json.loads(store.client.get_object(
                Bucket=store.bucket, Key=root + '/result.json')['Body'].read())
        except ClientError as error:
            if error.response['Error']['Code'] in {'NoSuchKey', '404'}:
                raise ValueError('HOLD stock compatibility pending for ' + label) from error
            raise
        if (result.get('verdict') != 'PASS' or result.get('role') != role
                or result.get('release_id') != baseline['release_id']):
            raise ValueError('missing verified stock compatibility for ' + label)
        if migration_kind == 'coordinator_native_isolated_sequential_prompt_v32_not_ab_experiment':
            gate = result.get('compatibility_gate', {})
            if (
                gate.get('trajectory_verified') is not True
                or gate.get('terminal_output_verified') is not True
                or gate.get('ask_user_calls') != 0
                or gate.get('extra_function_calls') != 0
                or gate.get('terminal_state') not in {
                    'completed', 'TASK_STATE_COMPLETED', 'COMPLETED'
                }
            ):
                raise ValueError('missing exact native Coordinator gate for ' + label)


def begin(store, driver, prepared, recheck):
    baseline=release(prepared['baseline'])
    state,etag=store.read()
    if state.get('baseline_cutover',{}).get('preparation_checksum')==digest(prepared):
        if state['phase']=='BASELINE_CUTOVER' or state.get('activated'):
            return state,etag
    active_revision=prepared.get('migration_kind') in ACTIVE_REVISION_KINDS
    if active_revision:
        if not state.get('activated') or state['phase'] not in {'IDLE','COMPLETED','ROLLED_BACK'}:
            raise ValueError('active experiment cannot migrate')
        validate_active_revision(prepared, state, baseline)
        if not driver.verify_route(state,state['verified_weight'],state['route_revision']):
            raise ValueError('existing serving route not verified')
    elif state['phase']!='IDLE' or state.get('activated') or state.get('experiment_id'):
        raise ValueError('only an inactive bootstrap can migrate; explicit reconciliation required')
    if etag!=prepared['parent_state_etag'] or state['champion']['release_id']!=prepared['old_champion']:
        raise ValueError('STALE_BASELINE_PREPARATION')
    if baseline.get('scope')!='workflow':
        raise ValueError('workflow preparation required')
    if not active_revision and driver.kube('get','virtualservice','recsys-workflow-ab','--ignore-not-found','-o','name').strip():
        raise ValueError('unexpected existing workflow route; operator reconciliation required')
    driver.verify_release(baseline)
    recheck()
    intent={'preparation_checksum':digest(prepared),'baseline_release_id':baseline['release_id'],
        'old_bootstrap':deepcopy(state['champion']),'parent_state_etag':etag,'stage':'INTENDED'}
    if active_revision:
        intent['migration_kind']=prepared['migration_kind']
        if state.get('experiment_id'):
            archived=store.archive(state)
            state['history']=[*state.get('history',[]),archived]
            intent['previous_experiment']=archived
    state.update(phase='BASELINE_CUTOVER',baseline_cutover=intent,baseline=baseline,pending=baseline,
        releases={**state.get('releases',{}),baseline['release_id']:baseline},
        route_intent={'weight':0,'next_phase':'IDLE'},verified_weight=None)
    return state,store.write(state,etag)


def reconcile(store, driver, prepared, recheck):
    state,etag=store.read()
    intent=state.get('baseline_cutover',{})
    if intent.get('preparation_checksum')!=digest(prepared):
        raise ValueError('cutover intent does not match preparation')
    if state.get('activated') and state['phase']=='IDLE':
        recheck()
        driver.verify_release(state['champion'])
        return driver.verify_route(state,0,state['route_revision'])
    if state['phase']!='BASELINE_CUTOVER':
        raise ValueError('cutover no longer owns state')
    baseline=release(prepared['baseline'])
    if state['baseline']!=baseline or state['pending']!=baseline:
        raise ValueError('cutover baseline drift')
    recheck()
    revision=driver.route(state,0)
    if not driver.verify_route(state,0,revision):
        return False
    driver.verify_release(baseline)
    recheck()
    # The old bootstrap was never activated, so it is archived in the intent,
    # not advertised as a verified previous release for manual rollback.
    state.update(champion=baseline,phase='IDLE',activated=True,route_revision=revision,
        verified_weight=0,baseline_cutover={**intent,'stage':'COMMITTED'})
    if intent.get('migration_kind') in ACTIVE_REVISION_KINDS:
        state['previous']=intent['old_bootstrap']
        state.pop('experiment_id',None)
        state.update(cases={},events=[],gate={},gate_evidence={},gate_windows=[],offline_evidence={},observation={})
    store.write(state,etag)
    return True


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--image',required=True,
        help='Compatibility with preparation job: pass the exact preparation object key')
    args=p.parse_args()
    key=args.image
    if not re.fullmatch(r'workflow/baseline-preparations/[0-9a-f]{64}/[0-9]+\.json',key):
        raise ValueError('exact immutable preparation object key required')
    if not os.environ.get('BUILD_URL') or not os.environ.get('JENKINS_URL'):
        raise ValueError('Jenkins shared-lock cutover required')
    os.environ.update(json.loads(Path(os.environ['AB_ENV_FILE']).read_text()))
    os.environ.update(AB_SCOPE='workflow',AB_SECRET_NAME='recsys-workflow-runtime',
        AB_ROUTER_IMAGE=json.loads(command('kubectl','-n','kagent','get','deployment','recsys-workflow-router','-o','json'))['spec']['template']['spec']['containers'][0]['image'])
    check('workflow')
    store=StateStore('s3://recsys-llm-ab/workflow/state.json')
    prepared=json.loads(store.client.get_object(Bucket=store.bucket,Key=key)['Body'].read())
    baseline=release(prepared['baseline'])
    driver=Driver()
    from botocore.exceptions import ClientError
    body=json.dumps(baseline,sort_keys=True).encode()
    release_key='workflow/releases/'+baseline['release_id']+'.json'
    try:
        store.client.put_object(Bucket=store.bucket,Key=release_key,Body=body,ContentType='application/json',IfNoneMatch='*')
    except ClientError as exc:
        if exc.response['ResponseMetadata']['HTTPStatusCode']!=412:
            raise
        if store.client.get_object(Bucket=store.bucket,Key=release_key)['Body'].read()!=body:
            raise ValueError('immutable baseline publication conflict')
    check_stock_probes(store, baseline, prepared.get('migration_kind'))
    def recheck():
        check_stock_probes(store,baseline,prepared.get('migration_kind'))
        for item in prepared['provenance']['objects']:
            value=json.loads(driver.kube('get',item['kind'],item['name'],'-o','json'))
            if any(value['metadata'][k]!=item[k] for k in ('uid','resourceVersion')) or digest(value['spec'])!=item['spec_checksum']:
                raise ValueError('STALE_SNAPSHOT; workflow remains closed')
        helm=json.loads(command('helm','status','recsys-global-model-config','-n','kagent','-o','json'))
        if helm['version']!=prepared['provenance']['global_helm_revision'] or helm['info']['status']!='deployed':
            raise ValueError('global Helm revision drift')
    begin(store,driver,prepared,recheck)
    deadline=time.monotonic()+120
    while not reconcile(store,driver,prepared,recheck):
        if time.monotonic()>=deadline:
            raise ValueError('HOLD cutover intent pending Envoy; resume same preparation')
        time.sleep(2)
    driver.kube('apply','-f','-',stdin=json.dumps({'apiVersion':'v1','kind':'ConfigMap',
        'metadata':{'name':'recsys-workflow-activation','namespace':driver.namespace},
        'data':{'enabled':'true','entrypoint':'recsys-workflow-router','baseline_release_id':baseline['release_id']}}))
    report={'stage':'baseline_cutover','release_id':baseline['release_id'],'activated':True,
        'preparation_key':key,'build_url':os.environ['BUILD_URL'],'offline_requests':0,'synthetic_requests':0}
    Path('.llm-agent-cd').mkdir(exist_ok=True)
    Path('.llm-agent-cd/evaluation-preparation.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report))


if __name__=='__main__':
    main()
