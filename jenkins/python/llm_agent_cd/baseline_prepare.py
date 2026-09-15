"""Prepare and verify a new immutable baseline without activating any route."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
from .driver import Driver, command
from .provision import secret
from .release import digest
from .release_guard import check
from .state import StateStore
from .workflow_contract import (
    revise_baseline,
    revise_coordinator_native_sequential_baseline,
    revise_stock_runtime_and_b8646_serving,
)


def main():
    p=argparse.ArgumentParser();p.add_argument('--image',required=True)
    p.add_argument('--verify-serving',action='store_true')
    p.add_argument('--stock-b8646-serving',action='store_true')
    p.add_argument('--coordinator-prompt-migration',action='store_true')
    p.add_argument('--revise-active',action='store_true')
    args=p.parse_args()
    if args.stock_b8646_serving and not args.revise_active:
        p.error('--stock-b8646-serving requires --revise-active')
    if args.stock_b8646_serving and args.coordinator_prompt_migration:
        p.error('select exactly one active baseline migration')
    if args.coordinator_prompt_migration and not (args.revise_active and args.verify_serving):
        p.error('--coordinator-prompt-migration requires --revise-active --verify-serving')
    if args.revise_active and not (args.stock_b8646_serving or args.coordinator_prompt_migration):
        p.error('--revise-active requires a reviewed active baseline migration')
    if not os.environ.get('BUILD_URL') or not os.environ.get('JENKINS_URL'):
        raise ValueError('shared-lock Jenkins preparation required')
    os.environ.update(json.loads(Path(os.environ['AB_ENV_FILE']).read_text()))
    os.environ.update(AB_SCOPE='workflow',AB_SECRET_NAME='recsys-workflow-runtime',
        AB_ROUTER_IMAGE=json.loads(command('kubectl','-n','kagent','get','deployment','recsys-workflow-router','-o','json'))['spec']['template']['spec']['containers'][0]['image'])
    os.environ.setdefault('AB_KAGENT_GRPC_TARGET',
                          'kagent-controller.kagent.svc.cluster.local:8084')
    check()
    store=StateStore('s3://recsys-llm-ab/workflow/state.json');state,etag=store.read()
    if args.revise_active:
        if not state.get('activated') or state['phase'] not in {'IDLE','ROLLED_BACK','COMPLETED'}:
            raise ValueError('verified inactive/terminal workflow required for baseline revision')
    elif state['phase']!='IDLE' or state.get('activated'): raise ValueError('inactive bootstrap required')
    target=Path('.llm-agent-cd')/((
        'prompt-baseline-' if args.coordinator_prompt_migration else 'stock-baseline-'
    )+os.environ['BUILD_NUMBER'])
    target.mkdir(parents=True,exist_ok=False)
    # Snapshot uses the existing read-only Recommendation account. Workflow CD
    # never gains access to the legacy prefix or reads mutable defaults at serve time.
    if args.revise_active:
        if args.coordinator_prompt_migration:
            if state['champion'].get('runtime',{}).get('go_adk_image') != args.image:
                raise ValueError('prompt migration must preserve the active stock Go ADK image')
            release,audit=revise_coordinator_native_sequential_baseline(state['champion'])
        else:
            llm=json.loads(Path('configs/llm-ab/catalog/qwen3.5-0.8b-q4_0-b8646-stock-adk-cache-v2.json').read_text())
            release,audit=revise_stock_runtime_and_b8646_serving(
                state['champion'],llm,args.image,os.environ['AB_ROUTER_IMAGE'])
        objects=[]
        for kind,name in [('modelconfig','recsys-global-model-config'),
                *[('sandboxagent','recsys-'+r+'-agent-sandbox') for r in ('coordinator','context','recommendation')]]:
            value=json.loads(command('kubectl','-n','kagent','get',kind,name,'-o','json'))
            objects.append({'kind':kind,'name':name,**{k:value['metadata'][k] for k in ('uid','resourceVersion')},'spec_checksum':digest(value['spec'])})
        helm=json.loads(command('helm','status','recsys-global-model-config','-n','kagent','-o','json'))
        if helm['info']['status']!='deployed':raise ValueError('global Helm deploy unfinished')
        provenance={'source':'frozen_workflow_champion','objects':objects,'global_helm_revision':helm['version']}
    else:
        readonly=secret('kagent','recsys-llm-ab-runtime')
        snapshot=target/'global-snapshot.json'
        result=subprocess.run([sys.executable,'-m','jenkins.python.llm_agent_cd.workflow_snapshot','--output',str(snapshot)],
            env={**os.environ,**{k:readonly[k] for k in ('MODEL_STORE_ENDPOINT','AWS_ACCESS_KEY_ID','AWS_SECRET_ACCESS_KEY','AWS_DEFAULT_REGION')}},
            capture_output=True,text=True,timeout=180)
        if result.returncode: raise RuntimeError('fresh global snapshot rejected; no baseline deployed')
        release,audit=revise_baseline(json.loads(snapshot.read_text()))
        provenance=json.loads(Path(str(snapshot)+'.provenance.json').read_text())
    fixtures=json.loads(Path('configs/llm-ab/workflow-cases-v11.json').read_text())
    driver=Driver();driver.preflight(release, release, fixtures)
    def recheck():
        for item in provenance['objects']:
            value=json.loads(driver.kube('get',item['kind'],item['name'],'-o','json'))
            if any(value['metadata'][k]!=item[k] for k in ('uid','resourceVersion')) or digest(value['spec'])!=item['spec_checksum']:
                raise ValueError('STALE_SNAPSHOT before baseline publication')
        helm=json.loads(command('helm','status','recsys-global-model-config','-n','kagent','-o','json'))
        if helm['version']!=provenance['global_helm_revision'] or helm['info']['status']!='deployed':
            raise ValueError('STALE_SNAPSHOT global Helm revision')
    recheck()
    # Intent is create-only and separate from the champion pointer. A failed
    # readiness check leaves the serving entrypoint and old state untouched.
    intent={'stage':'PREPARING_BASELINE','baseline':release,'provenance':provenance,
        'parent_state_etag':etag,'old_champion':state['champion']['release_id'],'audit':audit,
        'build_url':os.environ['BUILD_URL'],'activated':False}
    if args.revise_active:intent['migration_kind']=audit['change_type']
    key='workflow/baseline-preparations/'+release['release_id']+'/'+os.environ['BUILD_NUMBER']+'.json'
    store.client.put_object(Bucket=store.bucket,Key=key,Body=json.dumps(intent,sort_keys=True).encode(),
        ContentType='application/json',IfNoneMatch='*')
    driver.deploy(release)
    driver.verify_release(release)
    probes=[]
    if args.verify_serving:
        if args.stock_b8646_serving or args.coordinator_prompt_migration:
            from .runtime_probe import readonly_prompt_baseline_probes, readonly_stock_probes
            # Prove every member uses the reviewed stock ADK before
            # issuing any diagnostic inference.
            from .workflow import members
            from .manifests import name as resource_name
            for member in members(release).values():
                agent_name=resource_name(member)
                live=json.loads(driver.kube('get','sandboxagent',agent_name,'-o','json'))
                templates=json.loads(driver.kube('get','actortemplates',
                    '-l','kagent.dev/sandbox-agent='+agent_name,'-o','json'))['items']
                desired=str(live['metadata']['generation'])
                active=[item for item in templates if not item['metadata'].get('deletionTimestamp')
                    and item['metadata'].get('annotations',{}).get('kagent.dev/desired-generation')==desired
                    and item.get('status',{}).get('phase')=='Ready'
                    and item['spec']['containers'][0]['image']==args.image]
                if len(active)!=1:
                    raise ValueError('stock Go ADK ActorTemplate attestation failed for '+agent_name)
            if args.coordinator_prompt_migration:
                probes.extend(readonly_prompt_baseline_probes(store,release,args.image))
            else:
                probes.extend(readonly_stock_probes(store,release,args.image))
        else:
            from .runtime_probe import readonly_serving_probe
            probes.append(readonly_serving_probe(store,release))
    recheck()
    after,after_etag=store.read()
    if after!=state or after_etag!=etag: raise ValueError('state changed during preparation')
    (target/'baseline.json').write_text(json.dumps(release,indent=2)+'\n')
    report={'stage':'baseline_preparation','release_id':release['release_id'],'runtime_image':args.image,
        'state_unchanged':True,'activated':False,'dispatch_enabled':False,
        'offline_requests':0,'synthetic_requests':0,'build_url':os.environ['BUILD_URL'],'intent_key':key}
    if args.coordinator_prompt_migration:
        report.update(migration_kind=audit['change_type'], prompt_checksum=audit['prompt_checksum'],
                      runtime_preflight_count=3)
    report['runtime_preflights']=probes
    Path('.llm-agent-cd/evaluation-preparation.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report))


if __name__=='__main__':main()
