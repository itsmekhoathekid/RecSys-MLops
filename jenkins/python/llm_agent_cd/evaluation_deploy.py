"""Jenkins-lock-only additive preparation. Never activates or promotes a release."""
import argparse
import json
import os
from pathlib import Path
import re
from .provision import secret, apply, kube
from .release import digest
from .release_guard import check
from .state import StateStore
from .driver import command
from .capacity import verify_capacity, workflow_job_reservations


def collector_metadata_plan(collector):
    if len(collector['data'])!=1: raise ValueError('unexpected collector config keys')
    key,content=next(iter(collector['data'].items()))
    anchor='"langfuse.observation.metadata.operation",'
    fields=['experiment_id','variant','release_id','config_id','llm_version_id','source','role','fixture_checksum','evaluator_version']
    missing=['"langfuse.observation.metadata.'+k+'"' for k in fields if '"langfuse.observation.metadata.'+k+'"' not in content]
    if missing and content.count(anchor)!=1: raise ValueError('collector allowlist anchor drift')
    return key, content.replace(anchor,anchor+' '+', '.join(missing)+',') if missing else content, bool(missing)


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--image',required=True)
    p.add_argument('--maintain-terminal',action='store_true')
    args=p.parse_args()
    if not os.environ.get('BUILD_URL') or not os.environ.get('JENKINS_URL'):
        raise ValueError('run through lock-protected Jenkins preparation job')
    if not re.fullmatch(r'asia-southeast1-docker.pkg.dev/recsys-mlops-506406/recsys/recsys-llm-ab-router@sha256:[0-9a-f]{64}',args.image):
        raise ValueError('operator-owned pinned evaluation image required')
    settings=json.loads(Path(os.environ['AB_ENV_FILE']).read_text())
    os.environ.update(settings)
    check()
    store=StateStore('s3://recsys-llm-ab/workflow/state.json')
    before,etag=store.read()
    if args.maintain_terminal:
        if before['phase'] not in {'IDLE','COMPLETED','ROLLED_BACK'} or not before.get('activated'):
            raise ValueError('tooling maintenance requires a verified activated idle/terminal workflow')
    elif before['phase']!='IDLE' or before.get('activated'):
        raise ValueError('preparation supports only inactive workflow bootstrap')
    nodes=json.loads(command('kubectl','get','nodes','-o','json'))['items']
    pods=json.loads(command('kubectl','get','pods','-A','-o','json'))['items']
    collector=json.loads(kube('observability','get','configmap','recsys-otel-collector-config','-o','json'))
    key,content,collector_changed=collector_metadata_plan(collector)
    # Reserve actual planned rollouts, not an unchanged collector. Helm may
    # surge both router and receiver; finite CronJobs can overlap these.
    reserved=[]
    # The chart uses maxSurge=0. Preserve the journaled live replica count;
    # replacing one at a time needs no additional pod reservation.
    router=json.loads(kube('kagent','get','deployment','recsys-workflow-router','-o','json'))
    if (router['spec'].get('replicas',0)<1
            or router.get('status',{}).get('readyReplicas')!=router['spec']['replicas']):
        raise ValueError('no-surge router maintenance requires every live replica Ready')
    router_replicas=router['spec']['replicas']
    workloads=[]
    if collector_changed: workloads.append(('observability','recsys-otel-collector'))
    receiver_exists=args.maintain_terminal and kube('kagent','get','deployment','recsys-workflow-trigger','--ignore-not-found','-o','name')
    trigger_image=''
    if receiver_exists:
        # Durable webhook delivery is already healthy and its profile/policy
        # overlay is content-addressed. Preserve its exact image so a router
        # telemetry repair does not cause an unrelated one-replica restart.
        trigger=json.loads(kube('kagent','get','deployment','recsys-workflow-trigger','-o','json'))
        trigger_image=trigger['spec']['template']['spec']['containers'][0]['image']
        if not re.fullmatch(r'.+@sha256:[0-9a-f]{64}',trigger_image):
            raise ValueError('existing trigger image is not immutable')
    for ns,name in workloads:
        obj=json.loads(kube(ns,'get','deployment',name,'-o','json'))
        reserved.append({'metadata':{'namespace':ns,'name':'surge-'+name},
                         'spec':{'replicas':1,'template':obj['spec']['template']}})
    reserved.extend(workflow_job_reservations(pods,worker=False,probe=True))
    free=verify_capacity(nodes,pods,reserved,{},headroom={'cpu':'200m','memory':'128Mi'})
    runtime=secret('kagent','recsys-workflow-runtime')
    lf=secret('langfuse','recsys-langfuse-runtime')
    name='recsys-workflow-evaluation'
    old=kube('kagent','get','secret',name,'--ignore-not-found','-o','json')
    if old and json.loads(old)['metadata'].get('labels',{}).get('recsys.ai/owner')!='llm-agent-cd':
        raise ValueError('foreign evaluation credential secret')
    apply('kagent',{'apiVersion':'v1','kind':'Secret','metadata':{'name':name,'labels':{'recsys.ai/owner':'llm-agent-cd'}},
        'stringData':{'AB_DATABASE_URL':runtime['AB_DATABASE_URL'],
            'LANGFUSE_BASE_URL':'http://langfuse-web.langfuse.svc.cluster.local:3000',
            'LANGFUSE_PUBLIC_KEY':lf['project-public-key'],'LANGFUSE_SECRET_KEY':lf['project-secret-key']}})
    command('helm','upgrade','recsys-workflow-ab','infra/helm/recsys-workflow-ab','-n','kagent','--reuse-values',
        '--set-string','image='+args.image,'--set','replicas='+str(router_replicas),'--set','evaluation.enabled=true',
        '--set-string','evaluation.secretName=recsys-workflow-evaluation',
        *(['--set-string','trigger.image='+trigger_image] if trigger_image else []),
        *([] if args.maintain_terminal else ['--set','evaluation.suspend=true','--set','trigger.enabled=false']),
        '--atomic','--wait','--timeout','10m')
    kube('kagent','rollout','status','deployment/recsys-workflow-router','--timeout=180s')
    from apps.agentic.llm_ab_router.database import Database
    Database(runtime['AB_DATABASE_URL']).migrate()
    # CI provisions the same JSON kept in Helm, preserving dashboard UID and
    # every other dashboard/config key. There is no Grafana UI-only mutation.
    dashboard=json.loads(Path('infra/helm/recsys-observability/dashboards/llm-ab-rollout.json').read_text())
    cm=json.loads(kube('observability','get','configmap','recsys-grafana-dashboard-llm-ab-rollout','-o','json'))
    old_dashboard=json.loads(cm['data']['llm-ab-rollout.json'])
    if old_dashboard['uid']!=dashboard['uid']: raise ValueError('dashboard UID drift')
    patch=[{'op':'test','path':'/metadata/resourceVersion','value':cm['metadata']['resourceVersion']},
           {'op':'replace','path':'/data/llm-ab-rollout.json','value':json.dumps(dashboard,indent=2)+'\n'}]
    # Dashboard JSON exceeds Linux's single-argument limit. Stream it instead
    # of placing it in argv (also keeps future sensitive payloads off ps).
    kube('observability','patch','configmap',cm['metadata']['name'],'--type=json','--patch-file=/dev/stdin',data=json.dumps(patch))
    if collector_changed:
        path='/data/'+key.replace('~','~0').replace('/','~1')
        kube('observability','patch','configmap',collector['metadata']['name'],'--type=json','--patch-file=/dev/stdin',data=json.dumps([
            {'op':'test','path':'/metadata/resourceVersion','value':collector['metadata']['resourceVersion']},
            {'op':'replace','path':path,'value':content}]))
        kube('observability','patch','deployment','recsys-otel-collector','--type=merge','-p',json.dumps({
            'spec':{'template':{'metadata':{'annotations':{'recsys.ai/evaluation-metadata':digest(content)}}}}}))
        kube('observability','rollout','status','deployment/recsys-otel-collector','--timeout=180s')
    after,after_etag=store.read()
    if etag!=after_etag or before!=after: raise ValueError('workflow state changed during preparation')
    report={'stage':'evaluation_preparation','image':args.image,'state_unchanged':True,
        'champion':before['champion']['release_id'],
        'dispatch_enabled':'preserved' if args.maintain_terminal else False,
        'evaluator_suspended':'preserved' if args.maintain_terminal else True,
        'terminal_tooling_maintenance':args.maintain_terminal,
        'collector_changed':collector_changed,
        'synthetic_requests':0,'offline_requests':0,'dashboard_uid':dashboard['uid'],
        'source_checksum':os.environ.get('AB_SOURCE_CHECKSUM'),
        'capacity_after_reserved_surge':{n:{k:str(v) for k,v in r.items()} for n,r in free.items()}}
    Path('.llm-agent-cd').mkdir(exist_ok=True)
    Path('.llm-agent-cd/evaluation-preparation.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report))


if __name__=='__main__':main()
