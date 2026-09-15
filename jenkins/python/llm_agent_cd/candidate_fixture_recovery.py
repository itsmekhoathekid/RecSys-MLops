"""Recover the candidate after a control-only failure caused by ambiguous fixture v2."""
import argparse
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import time

from .driver import Driver
from .provision import kube
from .release_guard import check
from .state import StateStore

REVIEW="reviewed-explicit-null-fixture-v3-recovery-v1"
EXPERIMENT="wf-2b904a38623aabbe6a4510b31be49d3d"
RELEASE="b0c946863001e0aa5d5c5b8e087e953863eb22c5a8a84f536f4eb89ca3f5e445"
CONTROL="8a62a3d15896940c535aab556e83e6ce00c1c5b98716f6ef079e93a245a425c6"
FIXTURE_SHA="836aa8f8ad4167f2a1ab54d8402c06269092de714d9a9609a98600f0522dc341"


def validate_recovery(state, evidence):
    if (state.get('phase')!='ROLLED_BACK' or state.get('experiment_id')!=EXPERIMENT
            or state.get('pending',{}).get('release_id')!=RELEASE
            or state.get('champion',{}).get('release_id')!=CONTROL
            or state.get('gate')!={'verdict':'FAIL','reason':'champion runtime/tool contract violation'}
            or state.get('cases')):
        raise ValueError('exact fixture-ambiguity rollback state required')
    offline=state.get('offline_evidence',{}).get('cases',[])
    if len(offline)!=6 or any(not r.get('synced') or (r.get('result') or {}).get('verdict')!='PASS' for r in offline):
        raise ValueError('six synced offline PASS records required')
    expected={
      ('live_test',CONTROL,'FAIL','child runtime error',True,True):2,
      ('live_test',RELEASE,'HOLD','operational evidence incomplete',False,False):1}
    if evidence.get('invocations')!=expected or evidence.get('submitted')!=3:
        raise ValueError('only reviewed control-failure/candidate-HOLD window may be recovered')
    if evidence.get('compatibility')!={('candidate','PASS',True):3,('control','PASS',True):3}:
        raise ValueError('offline compatibility evidence changed')
    if evidence.get('offline_confirmed')!=6:
        raise ValueError('offline score confirmation incomplete')
    if RELEASE in state.get('disabled',[]): return 'pending'
    events=[e for e in state.get('recovery_events',[]) if e.get('review')==REVIEW
            and e.get('failed_experiment')==EXPERIMENT and e.get('release_id')==RELEASE
            and e.get('inference_requests')==0]
    if len(events)!=1: raise ValueError('candidate enabled without exact fixture recovery evidence')
    return 'recovered'


def main():
    p=argparse.ArgumentParser();p.add_argument('--image',required=True);args=p.parse_args()
    if args.image!=REVIEW: raise ValueError('unreviewed fixture recovery')
    if not os.environ.get('BUILD_URL') or not os.environ.get('JENKINS_URL'):
        raise ValueError('common-lock Jenkins job required')
    os.environ.update(json.loads(Path(os.environ['AB_ENV_FILE']).read_text()))
    # Discover the live digest before constructing Driver: Driver intentionally
    # requires AB_ROUTER_IMAGE to be pinned at initialization time.
    router=json.loads(kube('kagent','get','deployment','recsys-workflow-router','-o','json'))
    os.environ.update(AB_SCOPE='workflow',AB_ROUTER_IMAGE=router['spec']['template']['spec']['containers'][0]['image'])
    check()
    fixture=Path('configs/llm-ab/workflow-cases-v3.json').read_bytes()
    provenance=json.loads(Path('configs/llm-ab/workflow-cases-v3.provenance.json').read_text())
    if hashlib.sha256(fixture).hexdigest()!=FIXTURE_SHA or provenance.get('fixture_file_sha256')!=FIXTURE_SHA:
        raise ValueError('reviewed fixture v3 checksum mismatch')
    store=StateStore('s3://recsys-llm-ab/workflow/state.json');state,etag=store.read();driver=Driver()
    with driver.db.connect() as c:
        rows=c.execute("""SELECT source,release_id,result->>'verdict' verdict,result->>'reason' reason,
          (result->>'error')::boolean error,(result->>'contract_failure')::boolean contract_failure,count(*) n
          FROM recsys_ab.invocations WHERE experiment_id=%s GROUP BY 1,2,3,4,5,6""",(EXPERIMENT,)).fetchall()
        invocations={(r['source'],r['release_id'],r['verdict'],r['reason'],r['error'],r['contract_failure']):r['n'] for r in rows}
        rows=c.execute("""SELECT variant,result->>'verdict' verdict,finished_at IS NOT NULL finished,count(*) n
          FROM recsys_ab.compatibility_requests WHERE experiment_id=%s GROUP BY 1,2,3""",(EXPERIMENT,)).fetchall()
        compatibility={(r['variant'],r['verdict'],r['finished']):r['n'] for r in rows}
        submitted=c.execute('SELECT submitted FROM recsys_ab.live_load_runs WHERE experiment_id=%s',(EXPERIMENT,)).fetchone()['submitted']
        confirmed=c.execute("""SELECT count(*) n FROM recsys_ab.evaluation_outbox WHERE experiment_id=%s
          AND metadata->>'source'='offline' AND confirmed_at IS NOT NULL""",(EXPERIMENT,)).fetchone()['n']
    observed={'invocations':invocations,'compatibility':compatibility,'submitted':submitted,'offline_confirmed':confirmed}
    status=validate_recovery(state,observed)
    route_state=deepcopy(state)
    if RELEASE not in route_state.setdefault('disabled',[]):route_state['disabled'].append(RELEASE)
    if not driver.verify_route(route_state,0,state['route_revision']): raise ValueError('rollback route not verified')
    driver.verify_release(state['pending'])
    record={'stage':'candidate_fixture_recovery','review':REVIEW,'failed_experiment':EXPERIMENT,
      'release_id':RELEASE,'counts':{'live_test':3,'control_fail':2,'candidate_hold':1,
      'synthetic':0,'offline':6,'offline_confirmed':6},'fixture_sha256':FIXTURE_SHA,
      'reason':'fixture v2 omitted candidate_item_ids although expected arguments required null',
      'build_url':os.environ['BUILD_URL'],'at':time.time(),'inference_requests':0}
    key='workflow/recoveries/'+EXPERIMENT+'/explicit-null-fixture-v3.json'
    if status=='pending':
        store.client.put_object(Bucket=store.bucket,Key=key,Body=json.dumps(record,sort_keys=True).encode(),
          ContentType='application/json',IfNoneMatch='*')
        updated=deepcopy(state);updated['disabled']=[rid for rid in state['disabled'] if rid!=RELEASE]
        updated.setdefault('recovery_events',[]).append({**record,'evidence_key':key});store.write(updated,etag)
    else:
        persisted=json.loads(store.client.get_object(Bucket=store.bucket,Key=key)['Body'].read())
        for field in ('stage','review','failed_experiment','release_id','counts','fixture_sha256','reason','inference_requests'):
            if persisted.get(field)!=record.get(field):raise ValueError('fixture recovery evidence mismatch')
        record=persisted
    after,_=store.read()
    if RELEASE in after.get('disabled',[]) or after['champion']!=state['champion']:
        raise ValueError('fixture recovery state verification failed')
    if not driver.verify_route(route_state,0,after['route_revision']):raise ValueError('route changed during fixture recovery')
    report={**record,'evidence_key':key,'state_recovered':True,'champion_unchanged':True,'route_unchanged':True}
    Path('.llm-agent-cd').mkdir(exist_ok=True)
    Path('.llm-agent-cd/evaluation-preparation.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report))


if __name__=='__main__':main()
