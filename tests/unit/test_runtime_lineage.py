from __future__ import annotations

import pytest

from metadata.governance_catalog import (
    BRONZE_URNS,
    KAFKA_TOPIC_URNS,
    POSTGRES_FEATURE_URNS,
    REDIS_FEATURE_URNS,
    SILVER_URNS,
    SOURCE_POSTGRES_URNS,
)
from metadata.ingest_datahub_governance import (
    cdc_ingestion,
    dp1,
    dp2,
    dp3,
    emit_job,
    streaming_features,
    verify_governance_coverage,
)
from metadata.runtime_lineage import build_event, read_latest_event, write_event


def _products():
    return (dp1(), dp2(), dp3(), cdc_ingestion(), streaming_features())


def _events(products):
    events = {}
    for product in products:
        product_datasets = [dataset.urn for dataset in product.datasets]
        for index, job in enumerate(product.jobs):
            inputs = product_datasets if index else []
            outputs = product_datasets if index == 0 else []
            if product.id == "CDC_INGESTION":
                inputs = list(SOURCE_POSTGRES_URNS.values())
                outputs = list(KAFKA_TOPIC_URNS.values())
            elif product.id == "STREAMING_FEATURES":
                inputs = [KAFKA_TOPIC_URNS["behavior_events"]]
                outputs = list(REDIS_FEATURE_URNS.values()) if job.id.endswith("online_store") else []
            events[(product.id, job.id)] = build_event(
                pipeline=product.id,
                job_id=job.id,
                run_id="run-coverage",
                event_type="START" if product.id == "STREAMING_FEATURES" else "COMPLETE",
                inputs=inputs,
                outputs=outputs,
                upstream_jobs=[product.jobs[0].id] if index else [],
            )
    return events


def test_catalog_contains_no_predeclared_lineage():
    for product in _products():
        assert all(not hasattr(dataset, "upstreams") for dataset in product.datasets)
        assert all(not hasattr(job, "inputs") for job in product.jobs)
        assert all(not hasattr(job, "outputs") for job in product.jobs)


def test_openlineage_event_round_trip_uses_observed_datasets(tmp_path):
    event = build_event(
        pipeline="DP2",
        job_id="ingest_stage",
        run_id="manual__runtime-proof",
        event_type="COMPLETE",
        inputs=BRONZE_URNS.values(),
        outputs=SILVER_URNS.values(),
    )
    write_event(event, root=str(tmp_path))

    observed = read_latest_event("DP2", "ingest_stage", root=str(tmp_path))
    assert observed["run"]["runId"] == event["run"]["runId"]
    assert {item["name"] for item in observed["inputs"]} == set(BRONZE_URNS.values())
    assert {item["name"] for item in observed["outputs"]} == set(SILVER_URNS.values())


def test_datahub_job_io_is_built_from_runtime_event():
    product = streaming_features()
    job = product.jobs[0]
    event = build_event(
        pipeline=product.id,
        job_id=job.id,
        run_id="scheduled__2026-07-12",
        event_type="COMPLETE",
        inputs=[KAFKA_TOPIC_URNS["behavior_events"]],
        outputs=[POSTGRES_FEATURE_URNS[table] for table in REDIS_FEATURE_URNS],
    )
    calls = []

    class Emitter:
        def emit(self, entity_urn, entity_type, aspect_name, aspect):
            calls.append((aspect_name, aspect))

    emit_job(Emitter(), "urn:li:dataFlow:(airflow,recsys_flink_stream_features,PROD)", job, event)
    io_aspect = next(aspect for name, aspect in calls if name == "dataJobInputOutput")
    assert io_aspect["inputDatasets"] == [KAFKA_TOPIC_URNS["behavior_events"]]
    assert set(io_aspect["outputDatasets"]) == {POSTGRES_FEATURE_URNS[table] for table in REDIS_FEATURE_URNS}


def test_coverage_gate_requires_every_runtime_job_and_contract(monkeypatch):
    import metadata.ingest_datahub_governance as governance

    products = _products()
    reports = {
        product.id: {
            "run_id": "run-coverage",
            "status": "SUCCESS",
            "datasets": {dataset.urn: {"status": "SUCCESS", "checks": []} for dataset in product.datasets},
        }
        for product in products
    }
    monkeypatch.setattr(governance, "read_report", lambda pipeline: reports[pipeline])

    coverage = verify_governance_coverage(products, _events(products))
    assert coverage["verified"] is True
    assert coverage["datasets"] == 51
    assert coverage["jobs"] == sum(len(product.jobs) for product in products)


def test_coverage_gate_rejects_missing_runtime_job(monkeypatch):
    import metadata.ingest_datahub_governance as governance

    products = _products()
    events = _events(products)
    events.pop(("DP2", "ingest_stage"))
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
            },
        },
    )

    with pytest.raises(RuntimeError, match="Missing runtime lineage for DP2.ingest_stage"):
        verify_governance_coverage(products, events)
