from contextlib import contextmanager
import os

from opentelemetry import trace
from opentelemetry.propagate import extract, inject
from jenkins.python.llm_agent_cd.release import digest


def configure():
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    if endpoint:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        provider = TracerProvider(
            resource=Resource.create(
                {"service.name": "recsys-workflow-router" if os.environ.get("AB_SCOPE") == "workflow" else "recsys-llm-ab-router", "service.namespace": "kagent"}
            )
        )
        provider.add_span_processor(
            BatchSpanProcessor(
                OTLPSpanExporter(endpoint=endpoint.rstrip("/") + "/v1/traces")
            )
        )
        trace.set_tracer_provider(provider)


@contextmanager
def invocation(headers, state, rid, source):
    r = state.get("releases", {}).get(rid, {})
    attributes = {
        "agent": "recsys-workflow-router" if r.get("scope") == "workflow" else "recsys-recommendation-router",
        "recsys.ab.experiment_id": state.get("experiment_id", "baseline"),
        "recsys.ab.release_id": rid,
        "recsys.ab.config_id": r.get("config_id", "unknown"),
        "recsys.ab.llm_version_id": r.get("llm_version_id", "unknown"),
        "recsys.ab.source": source,
        "recsys.ab.variant": "control" if rid == state.get('baseline',{}).get('release_id') else "candidate",
        "recsys.ab.role": "root",
        "recsys.ab.fixture_checksum": state.get('fixture_checksum',digest(state.get('fixtures',[]))),
        "recsys.ab.evaluator_version": state.get('policy',{}).get('evaluation_version','none'),
    }
    for key in ('experiment_id','variant','release_id','config_id','llm_version_id','source','role','fixture_checksum','evaluator_version'):
        attributes['langfuse.observation.metadata.'+key] = attributes['recsys.ab.'+key]
    with trace.get_tracer(__name__).start_as_current_span(
        "workflow.ab.invocation" if r.get("scope") == "workflow" else "recommendation.ab.invocation", context=extract(headers), attributes=attributes
    ) as span:
        inject(headers)
        yield span
