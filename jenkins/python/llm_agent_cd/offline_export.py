"""Replayable OTLP export of completed fake-tool evidence, never inference."""
import json
from .release import digest


def export(client, snapshot, metadata):
    trace_id = metadata['trace_id']
    span_id = digest([trace_id,'root'])[:16]
    eid = metadata['experiment_id']+'-offline-'+metadata['variant']
    attributes = {
        'langfuse.environment':'experiment',
        'langfuse.experiment.id':eid, 'langfuse.experiment.name':eid,
        'langfuse.experiment.dataset.id':'compatibility-smoke-'+snapshot['fixture_checksum'],
        'langfuse.experiment.description':'3-case compatibility smoke; not full-workflow evaluation',
        'langfuse.experiment.item.id':digest([snapshot['fixture_checksum'],snapshot['case']]),
        'langfuse.experiment.item.root_observation_id':span_id,
        'langfuse.experiment.item.expected_output':json.dumps(snapshot['assertion']),
        'langfuse.observation.input':json.dumps({'messages':snapshot['messages'],'tools':snapshot['tools'],'generation':snapshot['generation']}),
        'langfuse.observation.output':json.dumps(snapshot.get('response',{'error_type':snapshot.get('error_type','UNKNOWN')})),
        'langfuse.observation.type':'generation',
        'langfuse.trace.name':'compatibility-smoke-'+snapshot['case'],
    }
    for k in ('experiment_id','variant','release_id','config_id','llm_version_id','fixture_checksum','source','case_id'):
        attributes['langfuse.observation.metadata.'+k] = metadata[k]
    spans = {'resourceSpans':[{'resource':{'attributes':[{'key':'service.name','value':{'stringValue':'recsys-workflow-offline'}}]},
        'scopeSpans':[{'scope':{'name':'recsys.compatibility.v1'},'spans':[{
            'traceId':trace_id,'spanId':span_id,'name':'compatibility-smoke-'+snapshot['case'],'kind':1,
            'startTimeUnixNano':str(snapshot['start_time_ns']),'endTimeUnixNano':str(snapshot['end_time_ns']),
            'attributes':[{'key':k,'value':{'stringValue':v}} for k,v in attributes.items()],
            'status':{'code':2 if snapshot.get('error_type') else 1}}]}]}]}
    result = client.post('/api/public/otel/v1/traces',json=spans,headers={'x-langfuse-ingestion-version':'4'})
    result.raise_for_status()
