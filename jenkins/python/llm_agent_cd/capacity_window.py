"""Retire non-serving adapters and move the unchanged telemetry probe to E2.

No backend/release/trace is deleted and no requests, limits, quota or gate is
lowered. The two exact adapter targets are recorded from the production audit.
"""
import argparse
from copy import deepcopy
import json
import os
from pathlib import Path
import time
from .capacity import verify_capacity, workflow_job_reservations
from .driver import command, Driver
from .provision import kube
from .release_guard import check
from .state import StateStore

RETIRED='5858209165ac799d82d4a97dc37e530fa7bbd13c6fbd1f1df742ac820a141fda'
FAILED='f9ae9556cd376832da28e06815d24036f2705dcdee5bd6611fbf6069f8e975a5'
FAILED_V2='9ed8dd2fb66751b865db6a7618a837a718e778c227d1849b8aeb19ed4a19f83c'
FAILED_NATIVE_PREP='3d31ce1dbe53ad3b9079c615ffd493935d6121861b32bc944949e92a1599667e'
FAILED_NATIVE_V1='99198a31b84b023bf220ceeb5d445e68bde0cd13d24b6afbbb62feb461761fcd'
FAILED_NATIVE_V2='f72c941e5eaee1c2697a1a8503465d2514dcc631312c41f69738f2dc13e16286'
FAILED_NATIVE_V3='aa4c787f99e11bc90e6ae4320a3ebf3fb2172c15f50cb0dbba0caf9258599c3e'
FAILED_NATIVE_COMPACT='ed7c236e1401c5cfb5a7552db1e38feb7c7b3c1a285607c251ef00c3ef45004c'
FAILED_NATIVE_COMPACT_WIRE='5cc2b0ccfa047a5d4c627838d2dda581b332182d4ab07cac5238365eecc72c72'
FAILED_NATIVE_NULL_SEMANTICS='8a62a3d15896940c535aab556e83e6ce00c1c5b98716f6ef079e93a245a425c6'
FAILED_NATIVE_BACKEND_V1='rec-llm-b59d09b04f8898759b8a'
FAILED_NATIVE_BACKEND_V2='rec-llm-32c8ebcefa95455c758f'
PROBE='recsys-llm-observability-probe'
TRIGGER='recsys-workflow-trigger'
SELECTOR={'recsys.ai/workload':'ml-system'}
TOLERATIONS=[{'key':'recsys.ai/workload','operator':'Equal','value':'ml-system','effect':'NoSchedule'}]


def reviewed_window(name):
    if name=='reviewed-capacity-window-v2':return FAILED,4,'full_experiment'
    if name=='reviewed-capacity-baseline-repair-v3':return FAILED_V2,2,'baseline_repair_only'
    if name=='reviewed-capacity-native-tools-v4':return FAILED_NATIVE_PREP,2,'full_experiment_native_tools'
    if name=='reviewed-capacity-native-tools-v5':return FAILED_NATIVE_V1,2,'full_experiment_native_tools_v2'
    if name=='reviewed-capacity-native-tools-v6':return FAILED_NATIVE_V2,2,'full_experiment_native_tools_v3'
    if name=='reviewed-capacity-native-tools-v7':return FAILED_NATIVE_V3,2,'full_experiment_native_tools_compact'
    if name=='reviewed-capacity-native-tools-v8':return FAILED_NATIVE_COMPACT,2,'full_experiment_native_tools_compact_wire'
    if name=='reviewed-capacity-native-tools-v9':return FAILED_NATIVE_COMPACT_WIRE,2,'full_experiment_native_tools_null_semantics'
    if name=='reviewed-capacity-native-tools-v10':return FAILED_NATIVE_NULL_SEMANTICS,2,'full_experiment_native_tools_empty_description'
    raise ValueError('unreviewed capacity window')


def native_reservations(include_backend=True):
    """Reviewed steady-state demand for native control plus both A/B arms."""
    ml={'recsys.ai/pool':'ml-system'}
    cpu={'recsys.ai/pool':'cpu-services'}
    def deployment(name,node,resources,*,init=None,tolerations=None):
        spec={'nodeSelector':node,'containers':[{'resources':{'requests':resources}}]}
        if init:spec['initContainers']=[{'resources':{'requests':init}}]
        if tolerations:spec['tolerations']=tolerations
        return {'metadata':{'namespace':'kagent','name':name},'spec':{'replicas':1,'template':{'spec':spec}}}
    result = [
        deployment('reserved-native-adapter-ml',ml,{'cpu':'25m','memory':'64Mi'},tolerations=TOLERATIONS),
        deployment('reserved-native-adapter-cpu',cpu,{'cpu':'25m','memory':'64Mi'}),
    ]
    if include_backend:
        result.insert(0,deployment('reserved-qwen35-native-control',ml,
            {'cpu':'100m','memory':'1536Mi'},init={'cpu':'50m','memory':'64Mi'},tolerations=TOLERATIONS))
    return result


def main():
    p=argparse.ArgumentParser();p.add_argument('--image',required=True)
    failed,new_replicas,scope=reviewed_window(p.parse_args().image)
    native=scope.startswith('full_experiment_native_tools')
    native_cleanup=scope in {'full_experiment_native_tools_v2','full_experiment_native_tools_v3',
                             'full_experiment_native_tools_compact',
                             'full_experiment_native_tools_compact_wire',
                             'full_experiment_native_tools_null_semantics',
                             'full_experiment_native_tools_empty_description'}
    reuse_backend=scope in {'full_experiment_native_tools_compact',
                            'full_experiment_native_tools_compact_wire',
                            'full_experiment_native_tools_null_semantics',
                            'full_experiment_native_tools_empty_description'}
    failed_backend_name=(FAILED_NATIVE_BACKEND_V1 if scope=='full_experiment_native_tools_v2'
        else FAILED_NATIVE_BACKEND_V2 if scope=='full_experiment_native_tools_v3' else None)
    if not os.environ.get('BUILD_URL') or not os.environ.get('JENKINS_URL'):raise ValueError('common-lock Jenkins job required')
    os.environ.update(json.loads(Path(os.environ['AB_ENV_FILE']).read_text()))
    os.environ.update(AB_SCOPE='workflow',AB_ROUTER_IMAGE=json.loads(kube('kagent','get','deployment','recsys-workflow-router','-o','json'))['spec']['template']['spec']['containers'][0]['image'])
    check();store=StateStore('s3://recsys-llm-ab/workflow/state.json');state,etag=store.read();driver=Driver()
    if state['phase']!='ROLLED_BACK':raise ValueError('verified rollback state required')
    if native:
        if (state.get('pending') or {}).get('release_id') not in state.get('disabled',[]):
            raise ValueError('failed experiment must remain quarantined')
        if failed in state.get('releases',{}) or any((state.get(k) or {}).get('release_id')==failed
                for k in ('champion','previous','baseline','pending')):
            raise ValueError('failed prepared baseline unexpectedly became serving state')
        retire_ids=(failed,)
    else:
        if failed not in state.get('disabled',[]):raise ValueError('verified quarantine required')
        retire_ids=(RETIRED,failed)
    if any((state.get(k) or {}).get('release_id') in set(retire_ids) for k in ('champion','previous')):
        raise ValueError('serving pointer is protected')
    with driver.db.connect() as c:
        if native and c.execute('SELECT count(*) AS n FROM recsys_ab.sessions WHERE release_id=%s',(failed,)).fetchone()['n']:
            raise ValueError('failed prepared baseline has sessions')
        if not native and c.execute('SELECT count(*) AS n FROM recsys_ab.sessions WHERE release_id=%s',(RETIRED,)).fetchone()['n']:
            raise ValueError('retired bootstrap has sessions')
        if c.execute('SELECT count(*) AS n FROM recsys_ab.invocations WHERE release_id=ANY(%s) AND finished_at IS NULL',(list(retire_ids),)).fetchone()['n']:
            raise ValueError('adapter has unfinished execution')
    if not driver.verify_route(state,0,state['route_revision']):raise ValueError('rollback route must be verified')
    adapter_targets=[('rec-ab-'+rid[:20],rid) for rid in retire_ids]
    if native_cleanup:adapter_targets.append(('rec-ab-'+failed[:20]+'-cpu',failed))
    adapters=[json.loads(kube('kagent','get','deployment',name,'-o','json')) for name,_ in adapter_targets]
    for obj,(_,rid) in zip(adapters,adapter_targets):
        env=[e for c in obj['spec']['template']['spec']['containers'] for e in c.get('env',[]) if e['name']=='RELEASE_ID']
        if obj['metadata'].get('labels',{}).get('recsys.ai/owner')!='llm-agent-cd' or env!=[{'name':'RELEASE_ID','value':rid}]:raise ValueError('adapter identity drift')
        allowed={0,1} if native_cleanup else {0,2}
        if obj['spec']['replicas'] not in allowed:raise ValueError('adapter replica drift')
    failed_backend=None
    if failed_backend_name is not None:
        failed_backend=json.loads(kube('kagent','get','deployment',failed_backend_name,'-o','json'))
        container=failed_backend['spec']['template']['spec']['containers']
        if (failed_backend['metadata'].get('labels',{}).get('recsys.ai/owner')!='llm-agent-cd'
                or failed_backend['spec']['replicas']!=1 or len(container)!=1 or container[0]['name']!='llama'
                or container[0]['resources']['requests']!={'cpu':'100m','memory':'1536Mi'}
                or '--chat-template-file' not in container[0]['args']
                or '--reasoning-budget' in container[0]['args']):
            raise ValueError('failed native backend identity drift')
    trigger=None
    if native:
        trigger=json.loads(kube('kagent','get','deployment',TRIGGER,'-o','json'))
        container=trigger['spec']['template']['spec']['containers']
        expected_trigger_replicas=1 if native_cleanup else 2
        if (trigger['metadata'].get('labels',{}).get('app.kubernetes.io/managed-by')!='Helm'
                or trigger['spec']['replicas']!=expected_trigger_replicas
                or trigger['status'].get('readyReplicas')!=expected_trigger_replicas
                or trigger['spec']['selector']['matchLabels']!={'app':TRIGGER}
                or len(container)!=1 or container[0]['name']!='receiver'
                or container[0]['resources']['requests']!={'cpu':'50m','memory':'128Mi'}):
            raise ValueError('workflow trigger identity/readiness drift')
    probe=json.loads(kube('observability','get','cronjob',PROBE,'-o','json'))
    if probe['spec']['concurrencyPolicy']!='Forbid' or probe['spec'].get('suspend'):raise ValueError('probe policy drift')
    nodes=json.loads(command('kubectl','get','nodes','-o','json'))['items']
    pods=json.loads(command('kubectl','get','pods','-A','-o','json'))['items']
    def retired_pod(p):
        return p['metadata']['namespace']=='kagent' and any(all(p['metadata'].get('labels',{}).get(k)==v for k,v in a['spec']['selector']['matchLabels'].items()) for a in adapters)
    def probe_pod(p):return p['metadata']['namespace']=='observability' and p['metadata'].get('labels',{}).get('app')==PROBE
    def trigger_pod(p):return native and p['metadata']['namespace']=='kagent' and all(
        p['metadata'].get('labels',{}).get(k)==v for k,v in trigger['spec']['selector']['matchLabels'].items())
    def backend_pod(p):return failed_backend is not None and p['metadata']['namespace']=='kagent' and all(
        p['metadata'].get('labels',{}).get(k)==v for k,v in failed_backend['spec']['selector']['matchLabels'].items())
    planned=[p for p in pods if not retired_pod(p) and not probe_pod(p)
        and (native_cleanup or not trigger_pod(p)) and not backend_pod(p)]
    reserve=workflow_job_reservations(planned,probe=True)
    if native:
        if not native_cleanup:
            trigger_reservation=deepcopy(trigger)
            trigger_reservation['spec']['replicas']=1
            reserve.append(trigger_reservation)
        reserve.extend(native_reservations(include_backend=not reuse_backend))
    else:
        # Baseline repair reserves one adapter pair; it is NOT full A/B capacity.
        # The later experiment still has to pass its own full capacity preflight.
        reserve.append({'metadata':{'namespace':'kagent','name':'new-workflow-adapters'},'spec':{'replicas':new_replicas,
            'template':{'spec':{'nodeSelector':SELECTOR,'tolerations':TOLERATIONS,
            'containers':[{'resources':{'requests':{'cpu':'25m','memory':'64Mi'}}}]}}}})
    projected=verify_capacity(nodes,planned,reserve,{},headroom={'cpu':'200m','memory':'128Mi'})
    key='workflow/capacity-windows/'+os.environ['BUILD_NUMBER']+'.json'
    snapshot={'build_url':os.environ['BUILD_URL'],'state_etag':etag,'adapters':adapters,'probe':probe,
        'trigger':trigger,'failed_backend':failed_backend,'capacity_scope':scope}
    store.client.put_object(Bucket=store.bucket,Key=key,Body=json.dumps(snapshot).encode(),ContentType='application/json',IfNoneMatch='*')
    for obj in adapters:
        if obj['spec']['replicas']:
            kube('kagent','patch','deployment',obj['metadata']['name'],'--type=json','-p',json.dumps([
                {'op':'test','path':'/metadata/resourceVersion','value':obj['metadata']['resourceVersion']},
                {'op':'replace','path':'/spec/replicas','value':0}]))
    if failed_backend is not None:
        kube('kagent','patch','deployment',failed_backend_name,'--type=json','-p',json.dumps([
            {'op':'test','path':'/metadata/resourceVersion','value':failed_backend['metadata']['resourceVersion']},
            {'op':'test','path':'/spec/replicas','value':1},
            {'op':'replace','path':'/spec/replicas','value':0}]))
    if trigger is not None and not native_cleanup:
        kube('kagent','patch','deployment',TRIGGER,'--type=json','-p',json.dumps([
            {'op':'test','path':'/metadata/resourceVersion','value':trigger['metadata']['resourceVersion']},
            {'op':'test','path':'/spec/replicas','value':2},
            {'op':'replace','path':'/spec/replicas','value':1}]))
    old_spec=probe['spec']['jobTemplate']['spec']['template']['spec']
    desired=deepcopy(old_spec);desired.update(nodeSelector=SELECTOR,tolerations=TOLERATIONS)
    if desired!=old_spec:
        kube('observability','patch','cronjob',PROBE,'--type=json','-p',json.dumps([
            {'op':'test','path':'/metadata/resourceVersion','value':probe['metadata']['resourceVersion']},
            {'op':'replace','path':'/spec/jobTemplate/spec/template/spec','value':desired}]))
    deadline=time.monotonic()+300
    while True:
        current=json.loads(command('kubectl','get','pods','-A','-o','json'))['items']
        outstanding=[p for p in current if p['status']['phase'] not in {'Succeeded','Failed'} and
            (retired_pod(p) or backend_pod(p) or (probe_pod(p) and p['spec'].get('nodeSelector')!=SELECTOR))]
        active_trigger=[p for p in current if p['status']['phase'] not in {'Succeeded','Failed'} and trigger_pod(p)]
        if not outstanding and (not native or len(active_trigger)<=1):break
        if time.monotonic()>deadline:raise ValueError('HOLD graceful pod termination; no force delete')
        time.sleep(5)
    if trigger is not None and not native_cleanup:
        kube('kagent','rollout','status','deployment/'+TRIGGER,'--timeout=300s')
    final_reserve=workflow_job_reservations(current,probe=True)
    if native:final_reserve.extend(native_reservations(include_backend=not reuse_backend))
    verify_capacity(nodes,current,final_reserve,{},headroom={'cpu':'200m','memory':'128Mi'})
    after,after_etag=store.read()
    if after!=state or after_etag!=etag or not driver.verify_route(after,0,after['route_revision']):raise ValueError('route/state preservation failure')
    report={'stage':'capacity_window','snapshot_key':key,'retired_adapter_replicas':0,'release_objects_and_backends_retained':True,
        'capacity_scope':scope,'full_experiment_capacity_verified':scope.startswith('full_experiment'),
        'native_control_and_existing_candidate_reserved':native,
        'workflow_trigger_replicas':1 if native else None,
        'failed_native_backend_replicas':0 if failed_backend is not None else None,
        'native_backend_reused':reuse_backend,
        'probe_enabled':True,'probe_resources_unchanged':True,'probe_node_selector':SELECTOR,'state_unchanged':True,
        'projected_headroom':{n:{k:str(v) for k,v in r.items()} for n,r in projected.items()}}
    Path('.llm-agent-cd').mkdir(exist_ok=True);Path('.llm-agent-cd/evaluation-preparation.json').write_text(json.dumps(report,indent=2)+'\n');print(json.dumps(report))


if __name__=='__main__':main()
