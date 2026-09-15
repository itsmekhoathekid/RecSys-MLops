"""Six durable compatibility calls. A retry may sync evidence, never re-infer."""
import json
import os
import re
import time
import httpx
from psycopg.types.json import Jsonb
from .release import digest
from .small_compatibility import generation_parameters, smoke_case_ids, smoke_tools, check

VERSION = 'compatibility-smoke-v1'


def frozen_fixtures(state):
    """Return the experiment-owned suite, never image-local mutable inputs."""
    fixtures = state.get('compatibility_fixtures')
    checksum = state.get('compatibility_fixture_checksum')
    suite = state.get('policy', {}).get('compatibility_suite', VERSION)
    if (not isinstance(fixtures, list) or len(fixtures) != 3
            or [row[0] for row in fixtures] != smoke_case_ids(suite)
            or checksum != digest(fixtures)):
        raise ValueError('missing or changed frozen compatibility fixtures')
    return fixtures


def frozen_tools(state):
    suite = state.get('policy', {}).get('compatibility_suite', VERSION)
    tools = state.get('compatibility_tools')
    checksum = state.get('compatibility_tools_checksum')
    # Preserve resumability for old workflow experiments created before tools
    # were explicitly snapshotted. New Recommendation experiments fail closed.
    if tools is None and suite == VERSION:
        return smoke_tools(suite)
    if (not isinstance(tools, list) or tools != smoke_tools(suite)
            or checksum != digest(tools)):
        raise ValueError('missing or changed frozen compatibility tools')
    return tools


def evaluate(snapshot):
    known = 'response' in snapshot or 'error_type' in snapshot
    passed = False
    if 'response' in snapshot:
        try: passed = check(snapshot['response']['choices'][0]['message'], snapshot['assertion'])
        except (ValueError,KeyError,TypeError,IndexError): pass
    verdict = 'PASS' if passed else 'FAIL' if known else 'HOLD'
    return {'evaluator_version':snapshot.get('evaluator_version', VERSION),'evidence_checksum':digest(snapshot),'verdict':verdict,
        'scores':[{'name':'compatibility_smoke','status':verdict if known else 'UNKNOWN',
                   'value':passed if known else None,'required':True,'reason':'direct inference with fake tools; not workflow acceptance'}]}


def run(db, state, call):
    # Reject stale/tampered persisted identities before issuing remaining calls.
    observation(db,state)
    fixtures = frozen_fixtures(state)
    tools = frozen_tools(state)
    checksum = digest(fixtures)
    evaluator_version = state.get('policy', {}).get('compatibility_suite', VERSION)
    for variant, key in [('control','baseline'),('candidate','pending')]:
        manifest = state[key]
        for case, messages, assertion in fixtures:
            request_key = digest([state['experiment_id'],evaluator_version,variant,case])
            with db.connect() as c:
                claimed = c.execute('''INSERT INTO recsys_ab.compatibility_requests
                    (request_key,experiment_id,variant,case_id,release_id,fixture_checksum)
                    VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING RETURNING request_key''',
                    (request_key,state['experiment_id'],variant,case,manifest['release_id'],checksum)).fetchone()
            if not claimed: continue  # Includes sent-but-ambiguous execution.
            start = time.time_ns()
            snapshot = {'kind':'compatibility','case':case,'messages':messages,'tools':tools,
                'assertion':assertion,'generation':generation_parameters(manifest), 'fixture_checksum':checksum,
                'start_time_ns':start,'real_tools_executed':0,'evaluator_version':evaluator_version}
            try:
                snapshot['response'] = call(manifest, variant, {'model':manifest['binding']['model_alias'],
                    'messages':messages,'tools':tools,**snapshot['generation']})
            except (httpx.HTTPError,ValueError,KeyError,IndexError) as exc:
                snapshot['error_type'] = type(exc).__name__
            snapshot['end_time_ns'] = time.time_ns()
            metadata = {'experiment_id':state['experiment_id'],'variant':variant,'request_key':request_key,
                'trace_id':digest([request_key,'trace'])[:32],'release_id':manifest['release_id'],
                'config_id':manifest['config_id'],'llm_version_id':manifest['llm_version_id'],
                'source':'offline','fixture_checksum':checksum,'case_id':case}
            with db.connect() as c:
                with c.transaction():
                    c.execute('UPDATE recsys_ab.compatibility_requests SET finished_at=now(),result=%s WHERE request_key=%s',
                              (Jsonb(evaluate(snapshot)),request_key))
                    c.execute('''INSERT INTO recsys_ab.evaluation_outbox(request_key,experiment_id,snapshot,metadata)
                        VALUES(%s,%s,%s,%s)''',(request_key,state['experiment_id'],Jsonb(snapshot),Jsonb(metadata)))


def observation(db, state):
    frozen_tools(state)
    with db.connect() as c:
        rows = c.execute('''SELECT r.variant,r.case_id,r.release_id,r.fixture_checksum,r.result,
            r.finished_at IS NOT NULL AS completed,e.confirmed_at IS NOT NULL AS synced
            FROM recsys_ab.compatibility_requests r LEFT JOIN recsys_ab.evaluation_outbox e USING(request_key)
            WHERE r.experiment_id=%s ORDER BY r.variant,r.case_id''',(state['experiment_id'],)).fetchall()
    fixtures = frozen_fixtures(state)
    expected = {(arm,case) for arm in ('control','candidate') for case,_,_ in fixtures}
    if {(r['variant'],r['case_id']) for r in rows} - expected: raise ValueError('unexpected offline case')
    expected_releases={'control':state['baseline']['release_id'],'candidate':state['pending']['release_id']}
    checksum=digest(fixtures)
    if any(r['release_id']!=expected_releases[r['variant']] or r['fixture_checksum']!=checksum for r in rows):
        raise ValueError('offline release/fixture evidence integrity mismatch')
    if any(r['result'] and r['result']['verdict']=='FAIL' for r in rows): verdict='FAIL'
    elif len(rows)==6 and all(r['result'] and r['result']['verdict']=='PASS' and r['synced'] for r in rows): verdict='PASS'
    else: verdict='HOLD'
    return {'verdict':verdict,'reason':'six compatibility smoke responses and score readback',
            'evaluator_version':state.get('policy', {}).get('compatibility_suite', VERSION),
            'fixture_checksum':checksum,'cases':rows}


def job(state, image, namespace='kagent'):
    eid = state['experiment_id']
    if not re.fullmatch(r'(?:wf|rec)-[0-9a-f]{32}',eid) or not re.fullmatch(r'.+@sha256:[0-9a-f]{64}',image):
        raise ValueError('offline requires a valid experiment ID and pinned runtime')
    manifests = {k: state[k] for k in (
        'experiment_id','baseline','pending','compatibility_fixtures',
        'compatibility_fixture_checksum')}
    for key in ('compatibility_tools', 'compatibility_tools_checksum'):
        if key in state:
            manifests[key] = state[key]
    manifests['policy'] = state.get('policy', {})
    env = [{'name':'AB_OFFLINE_MANIFESTS','value':json.dumps(manifests,sort_keys=True)}]
    for variant,key in [('control','baseline'),('candidate','pending')]:
        b=state[key]['binding']
        env.append({'name':variant.upper()+'_API_KEY','valueFrom':{'secretKeyRef':{'name':b['api_key_secret'],'key':b['api_key_secret_key']}}})
    runtime_secret = ('recsys-workflow-runtime'
                      if state['baseline'].get('scope') == 'workflow'
                      else 'recsys-llm-ab-runtime')
    return {'apiVersion':'batch/v1','kind':'Job','metadata':{'name':'ab-offline-'+eid,'namespace':namespace,'labels':{'app':'recsys-agent-offline'}},
        'spec':{'backoffLimit':0,'activeDeadlineSeconds':900,'ttlSecondsAfterFinished':86400,
        'template':{'metadata':{'labels':{'app':'recsys-agent-offline'},'annotations':{'sidecar.istio.io/inject':'false'}},
        'spec':{'restartPolicy':'Never','automountServiceAccountToken':False,
            'nodeSelector':{'recsys.ai/pool':'ml-system'},
            'tolerations':[{'key':'recsys.ai/workload','operator':'Equal','value':'ml-system','effect':'NoSchedule'}],
            'securityContext':{'runAsNonRoot':True,'runAsUser':1000},
            'containers':[{'name':'offline','image':image,'command':['python','-m','jenkins.python.llm_agent_cd.offline'],
                'envFrom':[{'secretRef':{'name':runtime_secret}}],'env':env,
                'resources':{'requests':{'cpu':'50m','memory':'128Mi'},'limits':{'cpu':'500m','memory':'256Mi'}}}]}}}}


def main():
    from apps.agentic.llm_ab_router.database import Database
    state = json.loads(os.environ['AB_OFFLINE_MANIFESTS'])
    def call(manifest, variant, body):
        b=manifest['binding']
        with httpx.Client(timeout=120,follow_redirects=False,transport=httpx.HTTPTransport(retries=0)) as c:
            r=c.post(b['backend_url'].rstrip('/')+'/chat/completions',json=body,
                headers={**b.get('default_headers',{}),'Authorization':'Bearer '+os.environ[variant.upper()+'_API_KEY']})
            r.raise_for_status()
            return r.json()
    run(Database(os.environ['AB_DATABASE_URL']),state,call)


if __name__=='__main__': main()
