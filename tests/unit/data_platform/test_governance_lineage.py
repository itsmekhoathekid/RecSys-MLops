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
    Dataset,
    batch_set_data_product,
    cdc_ingestion,
    dp1,
    dp2,
    dp3,
    emit_dataset,
    emit_dataset_contract,
    emit_job,
    schema_metadata,
    streaming_features,
    validation_result,
    verify_governance_coverage,
)
from metadata.governance_schemas import RAW_TABLE_SCHEMAS, SchemaColumn
from metadata.runtime_lineage import build_event, read_latest_event, write_event
from validate.governance_contracts import dataset_result, read_report, write_report


def test_governance_products_use_rubric_flow_ids_and_single_dataset_ownership():
    products = (dp1(), dp2(), dp3(), cdc_ingestion(), streaming_features())
    assert [product.flow_id for product in products] == [
        "recsys_dp1_raw_to_bronze",
        "recsys_dp2_bronze_to_silver_gold",
        "recsys_dp3_offline_feature_table",
        "recsys_cdc_postgres_to_kafka",
        "recsys_flink_stream_features",
    ]

    ownership: dict[str, list[str]] = {}
    for product in products:
        for dataset in product.datasets:
            ownership.setdefault(dataset.urn, []).append(product.id)
    assert all(len(owners) == 1 for owners in ownership.values())
    assert set(BRONZE_URNS.values()).issubset(
        {dataset.urn for dataset in dp1().datasets}
    )
    assert not hasattr(dp1().jobs[0], "inputs")
    assert all("/raw/" not in dataset.urn for dataset in dp1().datasets)
    assert all(
        "urn:li:dataPlatform:iceberg" in dataset.urn for dataset in dp1().datasets
    )
    assert [job.id for job in dp1().jobs] == [
        "ingest_stage",
        "optimize_stage",
        "validate_stage",
    ]
    assert [job.id for job in dp2().jobs] == [
        "ingest_stage",
        "optimize_stage",
        "validate_stage",
    ]
    assert set(SILVER_URNS.values()) == {dataset.urn for dataset in dp2().datasets}
    assert set(POSTGRES_FEATURE_URNS.values()).issubset(
        {dataset.urn for dataset in dp3().datasets}
    )
    assert set(REDIS_FEATURE_URNS.values()) == {
        dataset.urn for dataset in streaming_features().datasets
    }


def test_datajob_lineage_comes_only_from_runtime_event():
    product = streaming_features()
    offline_job = product.jobs[0]
    event = build_event(
        pipeline=product.id,
        job_id=offline_job.id,
        run_id="scheduled__2026-07-12",
        event_type="COMPLETE",
        inputs=[KAFKA_TOPIC_URNS["behavior_events"]],
        outputs=[
            POSTGRES_FEATURE_URNS["user_sequence_features"],
            POSTGRES_FEATURE_URNS["user_aggregate_features"],
            POSTGRES_FEATURE_URNS["item_features"],
        ],
    )
    calls = []

    class Emitter:
        def emit(self, entity_urn, entity_type, aspect_name, aspect):
            calls.append((entity_urn, entity_type, aspect_name, aspect))

    emit_job(
        Emitter(),
        "urn:li:dataFlow:(airflow,recsys_flink_stream_features,PROD)",
        offline_job,
        event,
    )
    io_aspect = next(call[3] for call in calls if call[2] == "dataJobInputOutput")
    assert io_aspect["inputDatasets"] == [KAFKA_TOPIC_URNS["behavior_events"]]
    assert set(io_aspect["outputDatasets"]) == set(
        POSTGRES_FEATURE_URNS.values()
    ).difference({POSTGRES_FEATURE_URNS["ml_ranking_labels"]})
    info = next(call[3] for call in calls if call[2] == "dataJobInfo")
    assert info["customProperties"]["lineage_source"] == "OpenLineage runtime event"
    assert info["customProperties"]["airflow_run_id"] == "scheduled__2026-07-12"


def test_empty_upstream_lineage_is_always_upserted(monkeypatch):
    calls = []

    class Emitter:
        def emit(self, entity_urn, entity_type, aspect_name, aspect):
            calls.append((entity_urn, entity_type, aspect_name, aspect))

    dataset = Dataset(
        urn="urn:li:dataset:(urn:li:dataPlatform:s3,source,PROD)",
        name="source",
        description="source",
        tags=("DP1",),
        custom_properties={},
        schema=(SchemaColumn("id", "BIGINT"),),
    )
    emit_dataset(Emitter(), dataset)
    lineage = [call for call in calls if call[2] == "upstreamLineage"]
    assert lineage == [
        (
            dataset.urn,
            "dataset",
            "upstreamLineage",
            {"upstreams": [], "fineGrainedLineages": []},
        )
    ]


def test_openlineage_event_round_trip_keeps_runtime_identity_and_observed_io(tmp_path):
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

    assert observed["eventType"] == "COMPLETE"
    assert observed["run"]["runId"] == event["run"]["runId"]
    assert {item["name"] for item in observed["inputs"]} == set(BRONZE_URNS.values())
    assert {item["name"] for item in observed["outputs"]} == set(SILVER_URNS.values())


def _runtime_events_for_all_products(products):
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
                outputs = (
                    list(REDIS_FEATURE_URNS.values())
                    if job.id.endswith("online_store")
                    else []
                )
            events[(product.id, job.id)] = build_event(
                pipeline=product.id,
                job_id=job.id,
                run_id="run-coverage",
                event_type="START"
                if product.id == "STREAMING_FEATURES"
                else "COMPLETE",
                inputs=inputs,
                outputs=outputs,
                upstream_jobs=[product.jobs[0].id] if index else [],
            )
    return events


def test_governance_verifier_requires_all_runtime_lineage_and_contracts(monkeypatch):
    import metadata.ingest_datahub_governance as governance

    products = (dp1(), dp2(), dp3(), cdc_ingestion(), streaming_features())
    events = _runtime_events_for_all_products(products)
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

    coverage = verify_governance_coverage(products, events)

    assert coverage["verified"] is True
    assert coverage["datasets"] == 51
    assert coverage["jobs"] == sum(len(product.jobs) for product in products)
    assert set(coverage["contracts"]) == {product.id for product in products}


def test_governance_verifier_rejects_missing_runtime_job(monkeypatch):
    import metadata.ingest_datahub_governance as governance

    products = (dp1(), dp2(), dp3(), cdc_ingestion(), streaming_features())
    events = _runtime_events_for_all_products(products)
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

    with pytest.raises(
        RuntimeError, match="Missing runtime lineage for DP2.ingest_stage"
    ):
        verify_governance_coverage(products, events)


def test_data_product_resources_are_replaced_with_exact_canonical_assets():
    calls = []

    class Emitter:
        def graphql(self, query, variables):
            calls.append(("graphql", variables))
            return {"batchSetDataProduct": True}

        def emit(self, entity_urn, entity_type, aspect_name, aspect):
            calls.append(("emit", entity_urn, entity_type, aspect_name, aspect))

    product = dp2()
    resources = ("urn:li:dataset:canonical", "urn:li:dataFlow:canonical")
    batch_set_data_product(Emitter(), product, "urn:li:dataProduct:dp2", resources)

    properties = calls[-1]
    assert properties[0:4] == (
        "emit",
        "urn:li:dataProduct:dp2",
        "dataProduct",
        "dataProductProperties",
    )
    assert [asset["destinationUrn"] for asset in properties[4]["assets"]] == list(
        resources
    )


@pytest.mark.parametrize(
    ("report", "expected"),
    [
        (
            {
                "run_id": "run-1",
                "datasets": {"dataset": {"status": "SUCCESS", "checks": []}},
            },
            "SUCCESS",
        ),
        (
            {
                "run_id": "run-2",
                "datasets": {"dataset": {"status": "FAILURE", "checks": []}},
            },
            "FAILURE",
        ),
        ({"run_id": "run-3", "datasets": {}}, "ERROR"),
    ],
)
def test_validation_result_maps_runtime_report(monkeypatch, report, expected):
    import metadata.ingest_datahub_governance as governance

    monkeypatch.setattr(governance, "read_report", lambda pipeline: report)
    dataset = Dataset(
        urn="dataset",
        name="dataset",
        description="dataset",
        tags=("DP1",),
        custom_properties={},
        schema=(SchemaColumn("id", "BIGINT"),),
        validation_pipeline="DP1",
    )
    status, _, _ = validation_result(dataset)
    assert status == expected


def test_every_governed_dataset_has_schema_and_valid_primary_keys():
    products = (dp1(), dp2(), dp3(), cdc_ingestion(), streaming_features())
    datasets = [dataset for product in products for dataset in product.datasets]

    assert len(datasets) == 51
    assert all(dataset.schema for dataset in datasets)
    assert all(dataset.validation_pipeline for dataset in datasets)
    assert all(dataset.custom_properties.get("contract") for dataset in datasets)
    for dataset in datasets:
        field_names = [column.name for column in dataset.schema]
        assert len(field_names) == len(set(field_names)), dataset.urn
        assert set(dataset.primary_keys).issubset(field_names), dataset.urn


def test_raw_governance_schema_matches_generator_column_names():
    import importlib.util
    from pathlib import Path

    schema_file = Path("apps/data-platform/data-generator/src/schemas.py")
    spec = importlib.util.spec_from_file_location("generator_schemas", schema_file)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert set(RAW_TABLE_SCHEMAS) == set(module.SCHEMAS)
    for table, arrow_schema in module.SCHEMAS.items():
        assert [
            column.name for column in RAW_TABLE_SCHEMAS[table]
        ] == arrow_schema.names


def test_schema_metadata_contains_columns_types_and_keys():
    dataset = next(
        item for item in dp1().datasets if item.name.endswith("bronze_behavior_events")
    )
    metadata = schema_metadata(dataset)
    fields = {field["fieldPath"]: field for field in metadata["fields"]}

    assert metadata["platform"] == "urn:li:dataPlatform:iceberg"
    assert metadata["primaryKeys"] == ["source_run_id", "event_id"]
    assert fields["event_id"]["type"] == {
        "type": {"com.linkedin.schema.StringType": {}}
    }
    assert fields["event_timestamp"]["type"] == {
        "type": {"com.linkedin.schema.TimeType": {}}
    }
    assert fields["quantity"]["type"] == {
        "type": {"com.linkedin.schema.NumberType": {}}
    }
    assert fields["source_run_id"]["isPartOfKey"] is True


def test_schema_contract_uses_native_data_schema_assertion(monkeypatch):
    import metadata.ingest_datahub_governance as governance

    calls = []

    class Emitter:
        def emit(self, entity_urn, entity_type, aspect_name, aspect):
            calls.append(("emit", entity_urn, entity_type, aspect_name, aspect))

        def graphql(self, query, variables):
            calls.append(("graphql", query, variables))
            if "upsertCustomAssertion" in query:
                return {"upsertCustomAssertion": {"urn": variables["urn"]}}
            if "upsertDataContract" in query:
                return {"upsertDataContract": {"urn": "urn:li:dataContract:test"}}
            return {"reportAssertionResult": True}

    dataset = next(
        item for item in dp1().datasets if item.name.endswith("bronze_behavior_events")
    )
    monkeypatch.setattr(
        governance,
        "validation_result",
        lambda _: (
            "SUCCESS",
            "run-1",
            [{"name": "required_columns", "status": "SUCCESS"}],
        ),
    )

    emit_dataset_contract(Emitter(), dataset)

    assertion_info = next(
        call for call in calls if call[0] == "emit" and call[3] == "assertionInfo"
    )[4]
    assert assertion_info["type"] == "DATA_SCHEMA"
    assert assertion_info["schemaAssertion"]["entity"] == dataset.urn
    assert assertion_info["schemaAssertion"]["schema"]["fields"]
    assert assertion_info["schemaAssertion"]["compatibility"] == "EXACT_MATCH"


def test_validation_report_round_trip_and_merge(tmp_path):
    root = str(tmp_path / "reports")
    first = write_report(
        "DP3",
        {
            "iceberg": dataset_result(
                [
                    {
                        "name": "row_count",
                        "status": "SUCCESS",
                        "expected": "> 0",
                        "observed": 5,
                    }
                ]
            )
        },
        run_id="run-1",
        root=root,
    )
    assert first["status"] == "SUCCESS"
    merged = write_report(
        "DP3",
        {
            "postgres": dataset_result(
                [
                    {
                        "name": "row_count",
                        "status": "FAILURE",
                        "expected": "> 0",
                        "observed": 0,
                    }
                ]
            )
        },
        run_id="run-1",
        root=root,
        merge_latest=True,
    )
    assert merged["status"] == "FAILURE"
    assert set(read_report("DP3", root=root)["datasets"]) == {"iceberg", "postgres"}


def test_validation_report_does_not_merge_datasets_from_an_old_run(tmp_path):
    root = str(tmp_path / "reports")
    write_report(
        "DP3",
        {
            "iceberg": dataset_result(
                [
                    {
                        "name": "row_count",
                        "status": "SUCCESS",
                        "expected": "> 0",
                        "observed": 5,
                    }
                ]
            )
        },
        run_id="run-1",
        root=root,
    )
    current = write_report(
        "DP3",
        {
            "postgres": dataset_result(
                [
                    {
                        "name": "row_count",
                        "status": "SUCCESS",
                        "expected": "> 0",
                        "observed": 5,
                    }
                ]
            )
        },
        run_id="run-2",
        root=root,
        merge_latest=True,
    )

    assert set(current["datasets"]) == {"postgres"}


def test_dp3_silver_source_does_not_rebuild_silver(monkeypatch):
    import features.spark.dp3_offline_feature_entrypoint as dp3

    class Spark:
        def stop(self):
            pass

    silver = {"clean_behavior_events": object()}
    outputs = {"recsys_features.feature_store.user_sequence_features": object()}
    monkeypatch.setattr(dp3, "spark_session", lambda name: Spark())
    monkeypatch.setattr(
        dp3,
        "load_config",
        lambda path: {
            "input": {"source": "silver_lakehouse"},
            "output": {"feast_postgres_export": {"enabled": False}},
            "features": {},
        },
    )
    monkeypatch.setattr(dp3, "create_spark_namespace", lambda spark, catalog: None)
    monkeypatch.setattr(
        dp3, "read_silver_lakehouse_tables", lambda spark, catalog: silver
    )
    monkeypatch.setattr(
        dp3,
        "build_silver_tables",
        lambda *args, **kwargs: pytest.fail("DP3 rebuilt Silver"),
    )
    monkeypatch.setattr(dp3, "_build_feature_outputs", lambda *args, **kwargs: outputs)
    monkeypatch.setattr(dp3, "write_iceberg_table", lambda *args, **kwargs: None)
    monkeypatch.setattr(dp3, "_write_postgres_tables", lambda *args, **kwargs: ())
    monkeypatch.setattr(
        dp3,
        "_write_dp3_iceberg_validation_report",
        lambda *args, **kwargs: {"status": "SUCCESS"},
    )
    monkeypatch.setattr(dp3, "row_count", lambda frame: 1)
    monkeypatch.setattr(
        dp3, "_output_summary", lambda frames: {"user_sequence_features": 1}
    )

    assert dp3.run_dp3_offline_features("unused.yaml") == {
        "clean_behavior_events": 1,
        "user_sequence_features": 1,
    }
