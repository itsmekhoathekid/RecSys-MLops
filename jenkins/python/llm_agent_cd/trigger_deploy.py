"""Locked webhook deployment. A separate explicit invocation enables dispatch."""
import argparse
import json
import os
from pathlib import Path
import secrets
from .provision import secret, apply, kube
from .driver import command
from .capacity import verify_capacity
from .release_guard import check
from .release import digest
from .state import StateStore


def source_overlay():
    """Publish trigger-only compatibility modules missing from the pinned base image."""
    data = {name: Path(path).read_text() for name, path in {
        'trigger.py': 'apps/agentic/llm_ab_router/trigger.py',
        'release.py': 'jenkins/python/llm_agent_cd/release.py',
        'workflow.py': 'jenkins/python/llm_agent_cd/workflow.py',
        'serving_profiles.py': 'jenkins/python/llm_agent_cd/serving_profiles.py',
    }.items()}
    checksum = digest(data)
    name = 'recsys-workflow-trigger-code-' + checksum[:12]
    desired = {'apiVersion':'v1','kind':'ConfigMap','metadata':{'name':name,
        'labels':{'recsys.ai/owner':'llm-agent-cd'},'annotations':{'recsys.ai/source-sha256':checksum}},
        'immutable':True,'data':data}
    old = kube('kagent','get','configmap',name,'--ignore-not-found','-o','json')
    if old:
        value=json.loads(old)
        if (value['metadata'].get('labels',{}).get('recsys.ai/owner')!='llm-agent-cd'
                or value['metadata'].get('annotations',{}).get('recsys.ai/source-sha256')!=checksum
                or value.get('immutable') is not True or value.get('data')!=data):
            raise ValueError('trigger source overlay identity conflict')
    else:
        apply('kagent',desired)
    return name, checksum


def credentials(enabled):
    name = 'recsys-workflow-trigger'
    old = kube('kagent','get','secret',name,'--ignore-not-found','-o','json')
    if old and json.loads(old)['metadata'].get('labels',{}).get('recsys.ai/owner') != 'llm-agent-cd':
        raise ValueError('foreign trigger secret')
    current = secret('kagent',name) if old else None
    runtime = secret('kagent','recsys-workflow-runtime')
    langfuse = secret('langfuse','recsys-langfuse-runtime')
    webhook = secret('kagent','recsys-workflow-webhook-auth')
    jenkins = secret('kagent','recsys-workflow-jenkins-dispatch')
    if not webhook or not jenkins or jenkins['AB_JENKINS_USER']!='recsys-workflow-dispatch':
        raise ValueError('verified webhook/scoped Jenkins credentials required')
    settings = {**(current or {}), **jenkins,
        'AB_DATABASE_URL':runtime['AB_DATABASE_URL'], 'AB_STATE_URI':runtime['AB_STATE_URI'],
        'MODEL_STORE_ENDPOINT':runtime['MODEL_STORE_ENDPOINT'], 'AWS_DEFAULT_REGION':'us-east-1',
        'AWS_ACCESS_KEY_ID':'recsys-workflow-trigger',
        'AWS_SECRET_ACCESS_KEY':current['AWS_SECRET_ACCESS_KEY'] if current else secrets.token_urlsafe(40),
        'AB_TRIGGER_STATUS_TOKEN':current['AB_TRIGGER_STATUS_TOKEN'] if current else secrets.token_urlsafe(40),
        'AB_DISPATCH_ENABLED':str(enabled).lower(), 'AB_ALLOW_LIVE_TEST':'true',
        'LANGFUSE_WEBHOOK_SECRET':webhook['LANGFUSE_WEBHOOK_SECRET'],
        'LANGFUSE_PROJECT_ID':webhook['project_id'], 'AB_LANGFUSE_PROMPT':webhook['prompt_name'],
        'LANGFUSE_BASE_URL':'http://langfuse-web.langfuse.svc.cluster.local:3000',
        'LANGFUSE_PUBLIC_KEY':langfuse['project-public-key'], 'LANGFUSE_SECRET_KEY':langfuse['project-secret-key']}
    # Read historical workflow/catalog, write candidate manifests only. Never
    # give the receiver the CD account or permission to change champion/state.
    policy={'Version':'2012-10-17','Statement':[
        {'Effect':'Allow','Action':['s3:GetBucketVersioning'],'Resource':['arn:aws:s3:::recsys-llm-ab']},
        {'Effect':'Allow','Action':['s3:GetObject','s3:GetObjectVersion'],'Resource':['arn:aws:s3:::recsys-llm-ab/workflow/*']},
        {'Effect':'Allow','Action':['s3:PutObject'],'Resource':['arn:aws:s3:::recsys-llm-ab/workflow/candidates/*']}]}
    # Persist once before account provisioning, so a lost response can resume
    # with the same password instead of invalidating already running pods.
    apply('kagent',{'apiVersion':'v1','kind':'Secret','metadata':{'name':name,
        'labels':{'recsys.ai/owner':'llm-agent-cd'}},'stringData':settings})
    script='''set -eu
read -r account
read -r password
read -r policy
directory=$(mktemp -d /tmp/workflow-trigger-mc.XXXXXX)
mc --config-dir "$directory" alias set owned http://127.0.0.1:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" >/dev/null
mc --config-dir "$directory" admin user add owned "$account" "$password" >/dev/null
printf '%s' "$policy" | mc --config-dir "$directory" admin policy create owned "$account" /dev/stdin >/dev/null
mc --config-dir "$directory" admin policy attach owned "$account" --user "$account" >/dev/null
rm -f -- "$directory/config.json"
rmdir -- "$directory/certs/CAs" "$directory/certs" "$directory" 2>/dev/null || true
'''
    kube('experiment-tracking','exec','-i','deployment/minio','--','sh','-c',script,
         data='\n'.join([settings['AWS_ACCESS_KEY_ID'],settings['AWS_SECRET_ACCESS_KEY'],json.dumps(policy)])+'\n')


def main():
    p=argparse.ArgumentParser();p.add_argument('--image',required=True)
    p.add_argument('--enable-dispatch',action='store_true');args=p.parse_args()
    if not os.environ.get('BUILD_URL') or not os.environ.get('JENKINS_URL'):
        raise ValueError('Jenkins shared-lock preparation required')
    os.environ.update(json.loads(Path(os.environ['AB_ENV_FILE']).read_text()))
    check('workflow')
    store=StateStore('s3://recsys-llm-ab/workflow/state.json');before,etag=store.read()
    if before['phase']!='IDLE' or before.get('experiment_id'):
        raise ValueError('only pre-experiment preparation allowed')
    if args.enable_dispatch and not before.get('activated'):
        raise ValueError('verified baseline cutover required before dispatch')
    image=json.loads(kube('kagent','get','deployment','recsys-workflow-router','-o','json'))['spec']['template']['spec']['containers'][0]['image']
    if args.image!=image or '@sha256:' not in image:raise ValueError('use existing pinned router image')
    nodes=json.loads(command('kubectl','get','nodes','-o','json'))['items']
    pods=json.loads(command('kubectl','get','pods','-A','-o','json'))['items']
    trigger_live=json.loads(kube('kagent','get','deployment','recsys-workflow-trigger','-o','json'))
    router_live=json.loads(kube('kagent','get','deployment','recsys-workflow-router','-o','json'))
    if (trigger_live['spec'].get('replicas',0)<1 or trigger_live.get('status',{}).get('readyReplicas')!=trigger_live['spec']['replicas']
            or router_live['spec'].get('replicas',0)<1 or router_live.get('status',{}).get('readyReplicas')!=router_live['spec']['replicas']):
        raise ValueError('receiver/router maintenance requires every live replica Ready')
    # A one-replica Deployment with the default 25% rolling strategy admits
    # one surge pod. Preserve live replica counts instead of restoring stale
    # Helm defaults. Full experiment capacity is checked separately.
    reserved=[{'metadata':{'name':'trigger-preparation','namespace':'kagent'},'spec':{'replicas':1,
        'template':{'spec':{'nodeSelector':{'recsys.ai/pool':'ml-system'},
            'tolerations':[{'key':'recsys.ai/workload','operator':'Equal','value':'ml-system','effect':'NoSchedule'}],
            'containers':[{'resources':{'requests':{'cpu':'50m','memory':'128Mi'}}}]}}}}]
    if args.enable_dispatch:
        pool=json.loads(kube('kagent','get','workerpool','recsys-workflow-router-pool','-o','json'))
        template=pool['spec']['template']
        reserved.append({'metadata':{'name':'facade-preparation','namespace':'kagent'},'spec':{
            'replicas':max(0,1-pool['spec'].get('replicas',0)), 'template':{'spec':{
                'nodeSelector':template.get('nodeSelector',{}),'tolerations':template.get('tolerations',[]),
                'containers':[{'resources':template['resources']}]}}}})
    verify_capacity(nodes,pods,reserved,{},headroom={'cpu':'200m','memory':'128Mi'})
    credentials(args.enable_dispatch)
    overlay, overlay_checksum = source_overlay()
    command('helm','upgrade','recsys-workflow-ab','infra/helm/recsys-workflow-ab','-n','kagent','--reuse-values',
        '--set','trigger.enabled=true','--set','trigger.suspendRecovery='+str(not args.enable_dispatch).lower(),
        '--set','replicas='+str(router_live['spec']['replicas']),
        '--set','trigger.replicas='+str(trigger_live['spec']['replicas']),
        '--set-string','trigger.revision=dispatch-'+str(args.enable_dispatch).lower(),
        '--set','trigger.ingress.enabled=true','--set-string','trigger.ingress.tlsSecretName=recsys-agents-tls',
        '--set','evaluation.suspend='+str(not args.enable_dispatch).lower(),
        '--set-string','trigger.sourceOverlayConfigMap='+overlay,
        *(['--set','facadePool.replicas=1'] if args.enable_dispatch else []),
        '--atomic','--wait','--timeout','5m')
    kube('kagent','rollout','status','deployment/recsys-workflow-trigger','--timeout=120s')
    after,after_etag=store.read()
    if after!=before or after_etag!=etag:raise ValueError('state changed during dispatch preparation')
    report={'stage':'dispatch_preparation','dispatch_enabled':args.enable_dispatch,
        'state_unchanged':True,'image':image,'source_overlay':overlay,
        'source_overlay_checksum':overlay_checksum,'inference_requests':0,'build_url':os.environ['BUILD_URL']}
    Path('.llm-agent-cd').mkdir(exist_ok=True)
    Path('.llm-agent-cd/evaluation-preparation.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report))


if __name__=='__main__':main()
