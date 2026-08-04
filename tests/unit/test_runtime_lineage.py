from __future__ import annotations

from dataclasses import replace

import pytest
from datahub.emitter.mcp import MetadataChangeProposalWrapper
from openlineage.client.event_v2 import RunEvent, RunState

from metadata.datahub_validation import publish_validation_results
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
    _emit_assertion,
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
    proposals = []

    class Emitter:
        def emit_mcp(self, proposal):
            proposals.append(proposal)

    emit_job(Emitter(), flow_urn(product.flow_id), product.jobs[0])
    assert all(isinstance(item, MetadataChangeProposalWrapper) for item in proposals)
    assert {item.aspectName for item in proposals} == {"dataJobInfo", "globalTags"}


def test_validation_publishes_directly_to_native_datahub_assertions(monkeypatch):
    calls = []

    class Graph:
        def upsert_custom_assertion(self, **kwargs):
            calls.append(("upsert", kwargs))
            return {"urn": kwargs["urn"]}

        def report_assertion_result(self, **kwargs):
            calls.append(("result", kwargs))
            return True

    monkeypatch.setenv("DATAHUB_VALIDATION_ENABLED", "true")
    dataset = next(iter(BRONZE_URNS.values()))
    payload = publish_validation_results(
        "DP1",
        {
            dataset: {
                "status": "SUCCESS",
                "checks": [
                    {
                        "name": "required_columns",
                        "status": "SUCCESS",
                        "expected": ["id"],
                        "observed": ["id"],
                    }
                ],
            }
        },
        run_id="native-run",
        graph=Graph(),
    )
    assert payload["datahub"]["published"] is True
    assert [kind for kind, _ in calls] == ["upsert", "result", "result"]
    assert all(call[1]["properties"][1]["value"] == "native-run" for call in calls[1:])


def test_governance_uses_native_graph_client_for_custom_assertion():
    dataset = dp1().datasets[0]

    class Graph:
        def upsert_custom_assertion(self, **kwargs):
            return {"urn": kwargs["urn"]}

    class Emitter:
        graph = Graph()

    assertion = _emit_assertion(Emitter(), dataset, assertion_type="DATA_QUALITY")
    assert assertion.startswith("urn:li:assertion:")


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
    assert clients[0].transport.config.url.endswith(
        "/openapi/openlineage/api/v1/lineage"
    )


def test_recorder_emits_start_and_complete(monkeypatch):
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


def test_recorder_does_not_fail_data_plane_when_datahub_is_unavailable(
    monkeypatch, caplog
):
    import metadata.runtime_lineage as runtime_lineage

    monkeypatch.setenv("RUNTIME_LINEAGE_STRICT", "false")
    monkeypatch.setattr(
        runtime_lineage,
        "emit_event",
        lambda event: (_ for _ in ()).throw(ConnectionError("down")),
    )
    with RuntimeLineageRecorder("DP2", "ingest_stage", run_id="run-datahub-down"):
        pass
    assert "Unable to publish START runtime lineage" in caplog.text
    assert "Unable to publish COMPLETE runtime lineage" in caplog.text


def test_coverage_gate_requires_every_governance_definition_and_contract():
    products = _products()
    coverage = verify_governance_coverage(products)
    assert coverage["verified"] is True
    assert coverage["datasets"] == sum(len(product.datasets) for product in products)
    assert coverage["jobs"] == sum(len(product.jobs) for product in products)
    assert coverage["validation"] == {
        "mode": "datahub-custom-assertion-writeback",
        "intermediate_reports": False,
    }


def test_coverage_gate_rejects_dataset_without_native_validation_definition():
    product = dp2()
    broken_dataset = replace(product.datasets[0], validation_pipeline=None)
    broken_product = replace(product, datasets=(broken_dataset, *product.datasets[1:]))
    with pytest.raises(RuntimeError, match="Missing validation pipeline"):
        verify_governance_coverage(
            (dp1(), broken_product, dp3(), cdc_ingestion(), streaming_features())
        )
