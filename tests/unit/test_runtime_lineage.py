from __future__ import annotations

import pytest
from datahub.emitter.mcp import MetadataChangeProposalWrapper
from openlineage.client.event_v2 import RunEvent, RunState

from metadata.governance_catalog import (
    BRONZE_URNS,
    ENV,
    ICEBERG_FEATURE_URNS,
    KAFKA_TOPIC_URNS,
    POSTGRES_FEATURE_URNS,
    REDIS_FEATURE_URNS,
    SILVER_URNS,
    SOURCE_POSTGRES_URNS,
    dataset_urn,
    flow_urn,
    job_urn,
    openlineage_job_name,
    pipeline_flow_id,
)
from metadata.ingest_datahub_governance import (
    _report_assertion_result,
    cdc_ingestion,
    dp1,
    dp2,
    dp3,
    emit_job,
    streaming_features,
    verify_governance_coverage,
)
from metadata.runtime_lineage import RuntimeLineageRecorder, build_event, emit_event


def _products():
    return (dp1(), dp2(), dp3(), cdc_ingestion(), streaming_features())


def test_catalog_contains_no_predeclared_lineage():
    for product in _products():
        assert all(not hasattr(dataset, "upstreams") for dataset in product.datasets)
        assert all(not hasattr(job, "inputs") for job in product.jobs)
        assert all(not hasattr(job, "outputs") for job in product.jobs)
        flow = flow_urn(product.flow_id)
        for job in product.jobs:
            assert job_urn(flow, job.id) == (
                f"urn:li:dataJob:({flow},{openlineage_job_name(product.id, job.id)})"
            )


def test_openlineage_event_maps_to_exact_catalog_dataset_and_job_urns():
    event = build_event(
        pipeline="DP2",
        job_id="ingest_stage",
        run_id="manual__runtime-proof",
        event_type="COMPLETE",
        inputs=BRONZE_URNS.values(),
        outputs=SILVER_URNS.values(),
    )
    assert isinstance(event, RunEvent)
    observed_inputs = {
        dataset_urn(item.namespace, item.name, ENV) for item in event.inputs
    }
    observed_outputs = {
        dataset_urn(item.namespace, item.name, ENV) for item in event.outputs
    }
    assert observed_inputs == set(BRONZE_URNS.values())
    assert observed_outputs == set(SILVER_URNS.values())
    assert event.job.namespace == ENV
    assert event.job.name == openlineage_job_name("DP2", "ingest_stage")
    flow = flow_urn(pipeline_flow_id("DP2"))
    assert job_urn(flow, "ingest_stage") == f"urn:li:dataJob:({flow},{event.job.name})"


def test_openlineage_dataset_identity_covers_every_governed_platform():
    catalog_urns = set().union(
        BRONZE_URNS.values(),
        SILVER_URNS.values(),
        ICEBERG_FEATURE_URNS.values(),
        POSTGRES_FEATURE_URNS.values(),
        SOURCE_POSTGRES_URNS.values(),
        KAFKA_TOPIC_URNS.values(),
        REDIS_FEATURE_URNS.values(),
    )
    event = build_event(
        pipeline="DP3",
        job_id="ingest_stage",
        run_id="all-platform-identities",
        event_type="COMPLETE",
        inputs=catalog_urns,
    )
    mapped_urns = {dataset_urn(item.namespace, item.name, ENV) for item in event.inputs}
    assert mapped_urns == catalog_urns


def test_governance_job_emission_leaves_lineage_to_native_openlineage():
    product = streaming_features()
    job = product.jobs[0]
    proposals = []

    class Emitter:
        def emit_mcp(self, proposal):
            proposals.append(proposal)

    emit_job(Emitter(), flow_urn(product.flow_id), job)
    assert all(isinstance(item, MetadataChangeProposalWrapper) for item in proposals)
    assert {item.aspectName for item in proposals} == {"dataJobInfo", "globalTags"}


def test_assertion_result_retries_until_async_mcp_is_visible(monkeypatch):
    calls = []

    class Emitter:
        def graphql(self, query, variables):
            calls.append((query, variables))
            if len(calls) < 3:
                raise RuntimeError("assertion does not exist or is not associated")
            return {"reportAssertionResult": True}

    monkeypatch.setenv("DATAHUB_ASSERTION_RESULT_MAX_ATTEMPTS", "3")
    monkeypatch.setenv("DATAHUB_ASSERTION_RESULT_RETRY_DELAY_SECONDS", "0")

    result = {"type": "SUCCESS", "timestampMillis": 1, "properties": []}
    _report_assertion_result(Emitter(), "urn:li:assertion:test", result)

    assert len(calls) == 3
    assert calls[-1][1] == {"urn": "urn:li:assertion:test", "result": result}


def test_assertion_result_raises_after_retry_budget(monkeypatch):
    class Emitter:
        def graphql(self, query, variables):
            raise RuntimeError("assertion does not exist or is not associated")

    monkeypatch.setenv("DATAHUB_ASSERTION_RESULT_MAX_ATTEMPTS", "2")
    monkeypatch.setenv("DATAHUB_ASSERTION_RESULT_RETRY_DELAY_SECONDS", "0")

    with pytest.raises(RuntimeError, match="does not exist"):
        _report_assertion_result(
            Emitter(),
            "urn:li:assertion:test",
            {"type": "ERROR", "timestampMillis": 1, "properties": []},
        )


def test_emit_event_posts_to_native_datahub_openlineage_endpoint(monkeypatch):
    import metadata.runtime_lineage as runtime_lineage

    clients = []

    class Client:
        def __init__(self, *, transport):
            self.transport = transport
            self.events = []
            self.closed = False
            clients.append(self)

        def emit(self, event):
            self.events.append(event)

        def close(self):
            self.closed = True

    monkeypatch.setattr(runtime_lineage, "OpenLineageClient", Client)
    monkeypatch.setenv("DATAHUB_GMS_URL", "http://datahub-gms:8080")
    monkeypatch.setenv("DATAHUB_TOKEN", "secret-token")
    event = build_event(
        pipeline="DP2",
        job_id="ingest_stage",
        run_id="run-direct",
        event_type="COMPLETE",
        inputs=BRONZE_URNS.values(),
        outputs=SILVER_URNS.values(),
    )

    assert emit_event(event) == event
    assert clients[0].events == [event]
    assert clients[0].closed is True
    assert clients[0].transport.config.url == (
        "http://datahub-gms:8080/openapi/openlineage/api/v1/lineage"
    )
    assert clients[0].transport.config.endpoint == ""
    assert clients[0].transport.config.auth.get_bearer() == "Bearer secret-token"
    assert clients[0].transport.config.timeout == 5.0


def test_openlineage_sdk_configures_transient_retries(monkeypatch):
    import metadata.runtime_lineage as runtime_lineage

    monkeypatch.setenv("RUNTIME_LINEAGE_MAX_ATTEMPTS", "3")
    monkeypatch.setenv("RUNTIME_LINEAGE_RETRY_DELAY_SECONDS", "0.25")

    client = runtime_lineage._openlineage_client()
    try:
        retry = client.transport.config.retry
        assert retry["total"] == 2
        assert retry["connect"] == 2
        assert retry["read"] == 2
        assert retry["backoff_factor"] == 0.25
        assert retry["allowed_methods"] == ["POST"]
        assert 429 in retry["status_forcelist"]
        assert 503 in retry["status_forcelist"]
    finally:
        client.close()


def test_recorder_emits_start_and_complete_with_known_runtime_datasets(monkeypatch):
    import metadata.runtime_lineage as runtime_lineage

    events = []
    monkeypatch.setattr(
        runtime_lineage, "emit_event", lambda event: events.append(event) or event
    )
    with RuntimeLineageRecorder(
        "DP2",
        "ingest_stage",
        inputs=set(BRONZE_URNS.values()),
        outputs=set(SILVER_URNS.values()),
        run_id="run-recorder",
    ):
        pass

    assert [event.eventType for event in events] == [RunState.START, RunState.COMPLETE]
    assert len(events[0].inputs) == len(BRONZE_URNS)
    assert len(events[0].outputs) == len(SILVER_URNS)
    assert len(events[1].inputs) == len(BRONZE_URNS)
    assert len(events[1].outputs) == len(SILVER_URNS)


def test_recorder_does_not_fail_data_plane_when_datahub_is_unavailable(
    monkeypatch, caplog
):
    import metadata.runtime_lineage as runtime_lineage

    monkeypatch.setenv("RUNTIME_LINEAGE_STRICT", "false")

    def unavailable(event):
        raise ConnectionError("down")

    monkeypatch.setattr(runtime_lineage, "emit_event", unavailable)

    with RuntimeLineageRecorder("DP2", "ingest_stage", run_id="run-datahub-down"):
        pass

    assert "Unable to publish START runtime lineage" in caplog.text
    assert "Unable to publish COMPLETE runtime lineage" in caplog.text


def test_coverage_gate_requires_every_governance_definition_and_contract(monkeypatch):
    import metadata.ingest_datahub_governance as governance

    products = _products()
    reports = {
        product.id: {
            "run_id": "run-coverage",
            "status": "SUCCESS",
            "datasets": {
                dataset.urn: {"status": "SUCCESS", "checks": []}
                for dataset in product.datasets
            },
        }
        for product in products
    }
    monkeypatch.setattr(governance, "read_report", lambda pipeline: reports[pipeline])

    coverage = verify_governance_coverage(products)
    assert coverage["verified"] is True
    assert coverage["datasets"] == 51
    assert coverage["jobs"] == sum(len(product.jobs) for product in products)


def test_coverage_gate_rejects_missing_contract_dataset(monkeypatch):
    import metadata.ingest_datahub_governance as governance

    products = _products()
    monkeypatch.setattr(
        governance,
        "read_report",
        lambda pipeline: {
            "run_id": "run-coverage",
            "status": "SUCCESS",
            "datasets": {
                dataset.urn: {"status": "SUCCESS", "checks": []}
                for product in products
                if product.id == pipeline
                for dataset in product.datasets
                if not (
                    pipeline == "DP2"
                    and dataset.urn == next(iter(SILVER_URNS.values()))
                )
            },
        },
    )

    with pytest.raises(RuntimeError, match="Data contract report DP2 is missing"):
        verify_governance_coverage(products)
