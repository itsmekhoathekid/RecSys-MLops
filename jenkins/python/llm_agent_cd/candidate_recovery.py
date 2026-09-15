"""Locked, create-only recovery of the missing global candidate index.

Use the latest source-backed active item rows and their original popularity
scores. Never invent personalized history, modify Feast, or replace an index
that a streaming writer has already recreated.
"""
import argparse
import json
import math
import os
from pathlib import Path

from .driver import command
from .release import digest
from .release_guard import check
from .state import StateStore

KEY = 'candidate:popular:global'
SQL = """SELECT json_agg(x ORDER BY product_id) FROM (
 SELECT DISTINCT ON (product_id) product_id,is_active,popularity_score,
 feature_timestamp,created_timestamp,feature_version,source_event_id
 FROM feature_store.item_features
 ORDER BY product_id,feature_timestamp DESC,created_timestamp DESC,source_event_id DESC
) x;"""


def plan(rows):
    if not isinstance(rows,list) or not 1 <= len(rows) <= 10000:
        raise ValueError('bounded nonempty source rows required')
    candidates={}
    for row in rows:
        if type(row['product_id']) is not int or row['product_id'] <= 0:
            raise ValueError('invalid source product ID')
        if row['is_active'] is not True: continue
        score=row['popularity_score']
        if type(score) not in (int,float) or not math.isfinite(score):
            raise ValueError('missing/nonfinite source popularity')
        # Spark batch rows legitimately have no individual source_event_id;
        # keep that null, pin the complete snapshot and both source timestamps.
        if str(row['product_id']) in candidates or not all(row.get(k) for k in ('feature_timestamp','feature_version','created_timestamp')):
            raise ValueError('ambiguous source identity/provenance')
        candidates[str(row['product_id'])]=float(score)
    if not candidates: raise ValueError('no active source items')
    return {'schema_version':1,'target':KEY,'source':'feature_store.item_features/latest-per-product',
            'rows':rows,'scores':candidates,'source_checksum':digest(rows)}


def main():
    p=argparse.ArgumentParser();p.add_argument('--image',required=True,
        help='inspect or exact source checksum, not a container image')
    args=p.parse_args()
    if not os.environ.get('BUILD_URL') or not os.environ.get('JENKINS_URL'):
        raise ValueError('shared-lock Jenkins required')
    os.environ.update(json.loads(Path(os.environ['AB_ENV_FILE']).read_text()))
    check()
    # SQL is fixed operator-owned text; credentials remain in the DB Pod.
    source=command('kubectl','-n','recsys-dataflow','exec','feature-postgres-0','-c','postgres','--',
        'sh','-c','psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc "$1"','recovery',SQL)
    snapshot=plan(json.loads(source))
    store=StateStore('s3://recsys-llm-ab/workflow/state.json')
    state,etag=store.read()
    if state['phase'] not in {'IDLE','ROLLED_BACK','COMPLETED'}:
        raise ValueError('experiment active; recovery blocked')
    report={'stage':'candidate_recovery','source_checksum':snapshot['source_checksum'],
        'source_rows':len(snapshot['rows']),'active_items':len(snapshot['scores']),
        'source_max_timestamp':max(r['feature_timestamp'] for r in snapshot['rows']),
        'target':KEY,'mode':'INSPECT','inference_requests':0,'build_url':os.environ['BUILD_URL']}
    if args.image!='inspect':
        if args.image!=snapshot['source_checksum']: raise ValueError('source changed since inspection')
        key='workflow/dependency-recovery/'+snapshot['source_checksum']+'/snapshot.json'
        from botocore.exceptions import ClientError
        body=json.dumps(snapshot,sort_keys=True).encode()
        try:
            store.client.put_object(Bucket=store.bucket,Key=key,Body=body,ContentType='application/json',IfNoneMatch='*')
        except ClientError as e:
            if e.response['ResponseMetadata']['HTTPStatusCode']!=412: raise
            if store.client.get_object(Bucket=store.bucket,Key=key)['Body'].read()!=body:
                raise ValueError('immutable recovery snapshot conflict')
        script='''import json,sys,redis
p=json.load(sys.stdin)
r=redis.Redis(host="redis.recsys-dataflow.svc.cluster.local",port=6379,db=0)
# One atomic transaction checks absence and creates the complete sorted set.
lua="if redis.call('EXISTS',KEYS[1])~=0 then return -1 end; for i=1,#ARGV,2 do redis.call('ZADD',KEYS[1],ARGV[i],ARGV[i+1]) end; return redis.call('ZCARD',KEYS[1])"
args=[v for k,s in p['scores'].items() for v in (str(s),k)]
n=r.eval(lua,1,p['target'],*args)
if n==-1:
 existing={k.decode():float(v) for k,v in r.zrange(p['target'],0,-1,withscores=True)}
 if existing!=p['scores']: raise ValueError('existing index differs; never overwrite')
print(json.dumps({'created':n!=-1,'count':r.zcard(p['target'])}))
'''
        outcome=json.loads(command('kubectl','-n','api-serving','exec','-i','deployment/recsys-online-feature-api','-c','api','--',
            'python','-c',script,stdin=json.dumps(snapshot)))
        if outcome['count']!=len(snapshot['scores']): raise ValueError('recovery count not verified')
        report.update(mode='RESTORED' if outcome['created'] else 'ALREADY_PRESENT',snapshot_key=key,verified_items=outcome['count'])
    after,after_etag=store.read()
    if after!=state or after_etag!=etag: raise ValueError('workflow state drift during recovery')
    Path('.llm-agent-cd').mkdir(exist_ok=True)
    Path('.llm-agent-cd/evaluation-preparation.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report))


if __name__=='__main__':main()
