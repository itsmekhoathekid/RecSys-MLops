"""One infrastructure trace and deterministic scores; no inference or tools."""
import argparse
import json
import time
from pathlib import Path
import httpx
from .provision import forward, secret
from .release import digest
from apps.agentic.llm_ab_router.evaluation_job import ScoreSync


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--probe-id', required=True)
    p.add_argument('--output', required=True)
    p.add_argument('--read-only', action='store_true')
    args = p.parse_args()
    target = Path(args.output)
    if target.exists(): raise ValueError('create-only evidence required')
    credentials = secret('langfuse', 'recsys-langfuse-runtime')
    trace_id = digest(['langfuse-preflight', args.probe_id])[:32]
    now = time.time_ns()
    def attr(key, value): return {'key':key, 'value':{'stringValue':value}}
    otel = {'resourceSpans':[{'resource':{'attributes':[attr('service.name','recsys-workflow-evaluation-preflight')]},
            'scopeSpans':[{'scope':{'name':'recsys.workflow.preflight'}, 'spans':[{
                'traceId':trace_id, 'spanId':digest([trace_id,'span'])[:16], 'name':'evaluation-score-roundtrip',
                'kind':1, 'startTimeUnixNano':str(now), 'endTimeUnixNano':str(now+1000000),
                'attributes':[attr('langfuse.trace.name','workflow-evaluation-infrastructure-preflight'),
                    attr('langfuse.observation.metadata.source','infrastructure_test')], 'status':{'code':1}}]}]}]}
    report = {'probe_id':args.probe_id,'trace_id':trace_id, 'source':'infrastructure_test',
              'inference_requests':0, 'scores':[], 'verdict':'HOLD'}
    with forward('langfuse', 'langfuse-web', 3000) as url:
        with httpx.Client(base_url=url, auth=(credentials['project-public-key'], credentials['project-secret-key']), timeout=20) as c:
            r = None if args.read_only else c.post('/api/public/otel/v1/traces', json=otel, headers={'x-langfuse-ingestion-version':'4'})
            report['trace_http_status'] = r.status_code if r else 'existing_trace'
            if args.read_only or r.is_success:
                for kind, value in [('BOOLEAN',1),('NUMERIC',1.5),('CATEGORICAL','NOT_APPLICABLE')]:
                    payload = {'id':digest([trace_id,kind]),'traceId':trace_id,'name':'preflight_'+kind.lower(),
                               'dataType':kind,'value':value,'metadata':{'source':'infrastructure_test','probe_id':args.probe_id}}
                    status = 'HOLD'
                    try:
                        if args.read_only: ScoreSync(c).readback(payload)
                        else: ScoreSync(c).confirm(payload)
                        status = 'PASS'
                    except (httpx.HTTPError, ValueError) as exc:
                        status = type(exc).__name__
                    # Safe shape diagnostics only, never arbitrary score/error content.
                    read = c.get('/api/public/v3/scores', params={'id':payload['id'],'fields':'details,subject'})
                    rows = read.json().get('data',[]) if read.is_success else []
                    report['scores'].append({'type':kind,'result':status,'read_status':read.status_code,
                        'row_keys':sorted(rows[0]) if rows else [],
                        'value_type':type(rows[0].get('value')).__name__ if rows else None,
                        'subject_keys':sorted(rows[0].get('subject',{})) if rows else []})
            if len(report['scores'])==3 and all(r['result']=='PASS' for r in report['scores']): report['verdict']='PASS'
    with target.open('x') as f: json.dump(report,f,indent=2)
    print(json.dumps(report))


if __name__ == '__main__': main()
