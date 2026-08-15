from __future__ import annotations

from dataclasses import replace

import pytest
from datahub.emitter.mcp import MetadataChangeProposalWrapper
from datahub.metadata.schema_classes import (
    DataJobInputOutputClass,
    DataProcessInstancePropertiesClass,
    DataProcessInstanceRunEventClass,
    DataProcessRunStatusClass,
)

from metadata.datahub_validation import publish_validation_results
from metadata.governance_catalog import (
    BRONZE_URNS,
    ICEBERG_FEATURE_URNS,
    KAFKA_TOPIC_URNS,
    POSTGRES_FEATURE_URNS,
    REDIS_FEATURE_URNS,
    SILVER_URNS,
    SOURCE_POSTGRES_URNS,
    flow_urn,
    job_urn,
    pipeline_flow_urn,
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
from metadata.runtime_lineage import RuntimeLineageRecorder, runtime_run_uuid


def _products():
    return (dp1(), dp2(), dp3(), cdc_ingestion(), streaming_features())


class _Emitter:
    def __init__(self, *, error=None):
        self.proposals = []
        self.error = error
        self.closed = False

    def emit(self, proposal, callback=None):
        if self.error:
            raise self.error
        self.proposals.append(proposal)

    def close(self):
        self.closed = True


def test_catalog_contains_no_predeclared_lineage_and_uses_native_job_ids():
    for product in _products():
        assert all(not hasattr(dataset, "upstreams") for dataset in product.datasets)
        assert all(not hasattr(job, "inputs") for job in product.jobs)
        assert all(not hasattr(job, "outputs") for job in product.jobs)
        flow = flow_urn(product.flow_id, orchestrator=product.orchestrator)
        for job in product.jobs:
            assert job_urn(flow, job.id) == f"urn:li:dataJob:({flow},{job.id})"


def test_pipeline_identity_uses_native_orchestrators_and_plugin_job_ids():
    assert pipeline_flow_urn("DP2") == (
        "urn:li:dataFlow:(airflow,recsys_dp2_bronze_to_silver_gold,PROD)"
    )
    assert pipeline_flow_urn("CDC_INGESTION") == (
        "urn:li:dataFlow:(kafka-connect,recsys_cdc_postgres_to_kafka,PROD)"
    )
    assert pipeline_flow_urn("STREAMING_FEATURES") == (
        "urn:li:dataFlow:(flink,recsys_flink_stream_features,PROD)"
    )
    assert job_urn(pipeline_flow_urn("DP2"), "ingest_stage").endswith(",ingest_stage)")


def test_datahub_sdk_dataset_identity_covers_every_governed_platform(monkeypatch):
    import metadata.runtime_lineage as runtime_lineage

    catalog_urns = set().union(
        BRONZE_URNS.values(),
        SILVER_URNS.values(),
        ICEBERG_FEATURE_URNS.values(),
        POSTGRES_FEATURE_URNS.values(),
        SOURCE_POSTGRES_URNS.values(),
        KAFKA_TOPIC_URNS.values(),
        REDIS_FEATURE_URNS.values(),
    )
    emitter = _Emitter()
    monkeypatch.setattr(runtime_lineage, "_runtime_emitter", lambda: emitter)
    recorder = RuntimeLineageRecorder(
        "STREAMING_FEATURES",
        "run_flink_stream_to_online_store",
        inputs=catalog_urns,
        run_id="all-platform-identities",
    )
    recorder.__enter__()
    instance = recorder.complete()
    assert instance is not None
    assert {str(item) for item in instance.inlets} == catalog_urns


def test_governance_job_emission_leaves_lineage_to_runtime_publishers():
    product = streaming_features()
    proposals = []

    class Emitter:
        def emit_mcp(self, proposal):
            proposals.append(proposal)

    emit_job(
        Emitter(),
        flow_urn(product.flow_id, orchestrator=product.orchestrator),
        product.jobs[0],
    )
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


def test_recorder_emits_start_success_and_exact_job_lineage(monkeypatch):
    import metadata.runtime_lineage as runtime_lineage

    emitter = _Emitter()
    monkeypatch.setattr(runtime_lineage, "_runtime_emitter", lambda: emitter)
    recorder = RuntimeLineageRecorder(
        "CDC_INGESTION",
        "register_debezium_connector",
        run_id="run-recorder",
    )
    recorder.__enter__()
    recorder.add_inputs(*SOURCE_POSTGRES_URNS.values())
    recorder.add_outputs(*KAFKA_TOPIC_URNS.values())
    instance = recorder.complete()

    assert instance is not None
    assert instance.id == runtime_run_uuid(
        "CDC_INGESTION", "register_debezium_connector", "run-recorder"
    )
    assert str(instance.template_urn) == job_urn(
        pipeline_flow_urn("CDC_INGESTION"), "register_debezium_connector"
    )
    events = [
        item.aspect
        for item in emitter.proposals
        if isinstance(item.aspect, DataProcessInstanceRunEventClass)
    ]
    assert [event.status for event in events] == [
        DataProcessRunStatusClass.STARTED,
        DataProcessRunStatusClass.COMPLETE,
    ]
    assert events[-1].result.type == "SUCCESS"
    lineage = [
        item
        for item in emitter.proposals
        if isinstance(item.aspect, DataJobInputOutputClass)
    ][-1]
    assert lineage.entityUrn == job_urn(
        pipeline_flow_urn("CDC_INGESTION"), "register_debezium_connector"
    )
    assert set(lineage.aspect.inputDatasets) == set(SOURCE_POSTGRES_URNS.values())
    assert set(lineage.aspect.outputDatasets) == set(KAFKA_TOPIC_URNS.values())
    assert emitter.closed is True


def test_recorder_emits_failure_with_error_property(monkeypatch):
    import metadata.runtime_lineage as runtime_lineage

    emitter = _Emitter()
    monkeypatch.setattr(runtime_lineage, "_runtime_emitter", lambda: emitter)
    with pytest.raises(RuntimeError, match="stream failed"):
        with RuntimeLineageRecorder(
            "STREAMING_FEATURES",
            "run_flink_stream_to_online_store",
            inputs={KAFKA_TOPIC_URNS["behavior_events"]},
            outputs=set(REDIS_FEATURE_URNS.values()),
            run_id="failed-stream",
        ):
            raise RuntimeError("stream failed")

    events = [
        item.aspect
        for item in emitter.proposals
        if isinstance(item.aspect, DataProcessInstanceRunEventClass)
    ]
    assert events[-1].result.type == "FAILURE"
    properties = [
        item.aspect
        for item in emitter.proposals
        if isinstance(item.aspect, DataProcessInstancePropertiesClass)
    ]
    assert properties[-1].customProperties["error"] == "stream failed"


def test_recorder_is_disabled_inside_airflow_pods(monkeypatch):
    import metadata.runtime_lineage as runtime_lineage

    monkeypatch.setenv("RUNTIME_LINEAGE_ENABLED", "false")
    monkeypatch.setattr(
        runtime_lineage,
        "_runtime_emitter",
        lambda: (_ for _ in ()).throw(AssertionError("must not create emitter")),
    )
    with RuntimeLineageRecorder("DP2", "ingest_stage"):
        pass


def test_recorder_does_not_fail_data_plane_when_datahub_is_unavailable(
    monkeypatch, caplog
):
    import metadata.runtime_lineage as runtime_lineage

    monkeypatch.setenv("RUNTIME_LINEAGE_STRICT", "false")
    monkeypatch.setattr(
        runtime_lineage,
        "_runtime_emitter",
        lambda: _Emitter(error=ConnectionError("down")),
    )
    with RuntimeLineageRecorder("DP2", "ingest_stage", run_id="run-datahub-down"):
        pass
    assert "Unable to publish STARTED DataHub SDK runtime lineage" in caplog.text
    assert "Unable to publish SUCCESS DataHub SDK runtime lineage" in caplog.text


def test_recorder_strict_mode_propagates_datahub_failure(monkeypatch):
    import metadata.runtime_lineage as runtime_lineage

    monkeypatch.setenv("RUNTIME_LINEAGE_STRICT", "true")
    monkeypatch.setattr(
        runtime_lineage,
        "_runtime_emitter",
        lambda: _Emitter(error=ConnectionError("strict-down")),
    )
    with pytest.raises(ConnectionError, match="strict-down"):
        RuntimeLineageRecorder("STREAMING_FEATURES", "job").__enter__()


def test_runtime_emitter_applies_timeout_retry_and_token_policy(monkeypatch):
    import metadata.runtime_lineage as runtime_lineage

    captured = {}
    monkeypatch.setenv("DATAHUB_GMS_URL", "http://datahub-gms:8080/")
    monkeypatch.setenv("DATAHUB_GMS_TOKEN", "secret")
    monkeypatch.setenv("RUNTIME_LINEAGE_HTTP_TIMEOUT_SECONDS", "7.5")
    monkeypatch.setenv("RUNTIME_LINEAGE_MAX_ATTEMPTS", "4")
    monkeypatch.setattr(
        runtime_lineage,
        "DatahubRestEmitter",
        lambda **kwargs: captured.update(kwargs) or captured,
    )

    runtime_lineage._runtime_emitter()
    assert captured["gms_server"] == "http://datahub-gms:8080"
    assert captured["token"] == "secret"
    assert captured["timeout_sec"] == 7.5
    assert captured["retry_max_times"] == 3
    assert 429 in captured["retry_status_codes"]
    assert 503 in captured["retry_status_codes"]


def test_dynamic_job_io_is_emitted_as_full_replacement(monkeypatch):
    import metadata.runtime_lineage as runtime_lineage

    emitter = _Emitter()
    monkeypatch.setattr(runtime_lineage, "_runtime_emitter", lambda: emitter)
    old_output = KAFKA_TOPIC_URNS["behavior_events"]
    new_output = KAFKA_TOPIC_URNS["orders"]

    with RuntimeLineageRecorder(
        "CDC_INGESTION",
        "register_debezium_connector",
        outputs={old_output},
        run_id="old-config",
    ):
        pass
    with RuntimeLineageRecorder(
        "CDC_INGESTION",
        "register_debezium_connector",
        outputs={new_output},
        run_id="new-config",
    ):
        pass

    lineage_aspects = [
        item.aspect
        for item in emitter.proposals
        if isinstance(item.aspect, DataJobInputOutputClass)
    ]
    assert lineage_aspects[-1].outputDatasets == [new_output]
    assert old_output not in lineage_aspects[-1].outputDatasets


def test_coverage_gate_requires_every_governance_definition_and_contract():
    products = _products()
    coverage = verify_governance_coverage(products)
    assert coverage["verified"] is True
    assert coverage["datasets"] == sum(len(product.datasets) for product in products)
    assert coverage["jobs"] == sum(len(product.jobs) for product in products)
    assert coverage["runtime_lineage"] == {
        "mode": "datahub-airflow-plugin+datahub-sdk",
        "airflow": "declared-inlets-outlets",
        "non_airflow": "data-process-instance",
    }
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
