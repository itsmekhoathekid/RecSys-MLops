"""Image-only Online Feature API repair through the common Jenkins locks."""
import argparse
from copy import deepcopy
import json
import os
from pathlib import Path
import yaml
from .capacity import verify_capacity, workflow_job_reservations
from .driver import command
from .provision import kube
from .release_guard import check
from .state import StateStore

NAME = 'recsys-online-feature-api'
NAMESPACE = 'api-serving'
CHART = 'infra/helm/recsys-online-feature-api'
IMAGE = ('asia-southeast1-docker.pkg.dev/recsys-mlops-506406/recsys/'
         'recsys-online-feature-api@sha256:5e9aacdf5eea02c2f309dd39cc6d72d71b8249bfd9be324e15ac88e348fc2b55')


def image_only(before, after, image):
    def index(text):
        return {(o['kind'],o['metadata']['name']):o for o in yaml.safe_load_all(text) if o}
    a,b=index(before),index(after)
    if a.keys()!=b.keys(): raise ValueError('serving resource inventory drift')
    containers=a[('Deployment',NAME)]['spec']['template']['spec']['containers']
    api=next(c for c in containers if c['name']=='api')
    api['image']=image
    next(e for e in api['env'] if e['name']=='IMAGE_REFERENCE')['value']=image
    if a!=b: raise ValueError('serving upgrade changes more than the reviewed image')


def main():
    p=argparse.ArgumentParser();p.add_argument('--image',required=True);args=p.parse_args()
    if args.image!=IMAGE or not os.environ.get('BUILD_URL') or not os.environ.get('JENKINS_URL'):
        raise ValueError('reviewed image and common-lock Jenkins job required')
    os.environ.update(json.loads(Path(os.environ['AB_ENV_FILE']).read_text()))
    check()
    store=StateStore('s3://recsys-llm-ab/workflow/state.json');before,etag=store.read()
    nodes=json.loads(command('kubectl','get','nodes','-o','json'))['items']
    pods=json.loads(command('kubectl','get','pods','-A','-o','json'))['items']
    live=json.loads(kube(NAMESPACE,'get','deployment',NAME,'-o','json'))
    # Reserve an actual admitted pod template, including its injected sidecar.
    selected=[p for p in pods if p['metadata']['namespace']==NAMESPACE
        and all(p['metadata'].get('labels',{}).get(k)==v for k,v in live['spec']['selector']['matchLabels'].items())
        and p['status']['phase']=='Running']
    if len(selected)!=1: raise ValueError('unexpected serving replicas/rollout; inspect first')
    spec=deepcopy(selected[0]['spec']);spec.pop('nodeName',None)
    reserve=[{'metadata':{'namespace':NAMESPACE,'name':'online-api-surge'},
              'spec':{'replicas':1,'template':{'spec':spec}}}]
    reserve.extend(workflow_job_reservations(pods,worker=False,probe=True))
    free=verify_capacity(nodes,pods,reserve,{},headroom={'cpu':'200m','memory':'128Mi'})
    values=json.loads(command('helm','get','values',NAME,'-n',NAMESPACE,'-a','-o','json'))
    old_manifest=command('helm','get','manifest',NAME,'-n',NAMESPACE)
    values['image']=IMAGE
    preview=command('helm','template',NAME,CHART,'-n',NAMESPACE,'--is-upgrade','--dry-run=server','-f','-',stdin=json.dumps(values))
    image_only(old_manifest,preview,IMAGE)
    previous=json.loads(command('helm','status',NAME,'-n',NAMESPACE,'-o','json'))
    if previous['info']['status']!='deployed': raise ValueError('unfinished serving Helm operation')
    command('helm','upgrade',NAME,CHART,'-n',NAMESPACE,'--reuse-values','--set-string','image='+IMAGE,
            '--atomic','--wait','--timeout','8m')
    kube(NAMESPACE,'rollout','status','deployment/'+NAME,'--timeout=180s')
    deployed=json.loads(kube(NAMESPACE,'get','deployment',NAME,'-o','json'))
    if next(c for c in deployed['spec']['template']['spec']['containers'] if c['name']=='api')['image']!=IMAGE:
        raise ValueError('serving image verification failed')
    after,after_etag=store.read()
    if after!=before or after_etag!=etag: raise ValueError('workflow state changed during serving repair')
    report={'stage':'serving_image_repair','image':IMAGE,'previous_helm_revision':previous['version'],
        'state_unchanged':True,'resource_config_unchanged':True,'inference_requests':0,
        'build_url':os.environ['BUILD_URL'],'source_checksum':os.environ.get('AB_SOURCE_CHECKSUM'),
        'capacity_after_reserved_surge':{n:{k:str(v) for k,v in r.items()} for n,r in free.items()}}
    Path('.llm-agent-cd').mkdir(exist_ok=True)
    Path('.llm-agent-cd/evaluation-preparation.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report))


if __name__=='__main__':main()
