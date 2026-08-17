from __future__ import annotations

import pytest
from datahub.emitter.mcp import MetadataChangeProposalWrapper

from metadata.governance_catalog import (
    BRONZE_URNS,
    POSTGRES_FEATURE_URNS,
    REDIS_FEATURE_URNS,
    RAG_MILVUS_URNS,
    SILVER_URNS,
)
from metadata.governance_schemas import RAW_TABLE_SCHEMAS, SchemaColumn
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
    rag_items,
    schema_metadata,
    streaming_features,
    verify_governance_coverage,
)


def test_governance_products_use_canonical_flow_ids_and_single_dataset_ownership():
    products = (dp1(), dp2(), dp3(), cdc_ingestion(), streaming_features(), rag_items())
    assert [product.flow_id for product in products] == [
        "recsys_dp1_raw_to_bronze",
        "recsys_dp2_bronze_to_silver_gold",
        "recsys_dp3_offline_feature_table",
        "recsys_cdc_postgres_to_kafka",
        "recsys_flink_stream_features",
        "recsys_rag_item_index",
    ]

    ownership: dict[str, list[str]] = {}
    for product in products:
        for dataset in product.datasets:
            ownership.setdefault(dataset.urn, []).append(product.id)
    assert all(len(owners) == 1 for owners in ownership.values())
    assert set(BRONZE_URNS.values()).issubset(
        {dataset.urn for dataset in dp1().datasets}
    )
    assert set(SILVER_URNS.values()) == {dataset.urn for dataset in dp2().datasets}
    assert set(POSTGRES_FEATURE_URNS.values()).issubset(
        {dataset.urn for dataset in dp3().datasets}
    )
    assert set(REDIS_FEATURE_URNS.values()) == {
        dataset.urn for dataset in streaming_features().datasets
    }
    assert set(RAG_MILVUS_URNS.values()).issubset(
        {dataset.urn for dataset in rag_items().datasets}
    )


def test_datajob_catalog_emission_does_not_duplicate_runtime_io():
    proposals = []

    class Emitter:
        def emit_mcp(self, proposal):
            proposals.append(proposal)

    product = streaming_features()
    emit_job(
        Emitter(),
        "urn:li:dataFlow:(flink,recsys_flink_stream_features,PROD)",
        product.jobs[0],
    )
    assert all(isinstance(item, MetadataChangeProposalWrapper) for item in proposals)
    assert {item.aspectName for item in proposals} == {"dataJobInfo", "globalTags"}


def test_empty_upstream_lineage_is_upserted_to_remove_stale_dataset_edges():
    proposals = []

    class Emitter:
        def emit_mcp(self, proposal):
            proposals.append(proposal)

    dataset = Dataset(
        urn="urn:li:dataset:(urn:li:dataPlatform:s3,source,PROD)",
        name="source",
        description="source",
        tags=("DP1",),
        custom_properties={},
        schema=(SchemaColumn("id", "BIGINT"),),
    )
    emit_dataset(Emitter(), dataset)
    lineage = [item for item in proposals if item.aspectName == "upstreamLineage"]
    assert len(lineage) == 1
    assert lineage[0].aspect.to_obj() == {
        "upstreams": [],
        "fineGrainedLineages": [],
    }


def test_governance_verifier_checks_native_definitions_without_minio_reports():
    products = (dp1(), dp2(), dp3(), cdc_ingestion(), streaming_features(), rag_items())
    coverage = verify_governance_coverage(products)
    assert coverage["verified"] is True
    assert coverage["datasets"] == 56
    assert coverage["jobs"] == sum(len(product.jobs) for product in products)
    assert coverage["runtime_lineage"]["mode"] == ("datahub-airflow-plugin+datahub-sdk")
    assert coverage["validation"]["intermediate_reports"] is False


def test_data_product_resources_are_replaced_with_exact_canonical_assets():
    graphql_calls = []
    proposals = []

    class Graph:
        def execute_graphql(self, query, variables):
            graphql_calls.append(variables)
            return {"batchSetDataProduct": True}

    class Emitter:
        graph = Graph()

        def emit_mcp(self, proposal):
            proposals.append(proposal)

    product = dp2()
    resources = ("urn:li:dataset:canonical", "urn:li:dataFlow:canonical")
    batch_set_data_product(Emitter(), product, "urn:li:dataProduct:dp2", resources)

    assert graphql_calls[-1]["input"]["resourceUrns"] == list(resources)
    properties = next(
        item for item in proposals if item.aspectName == "dataProductProperties"
    )
    assert [
        asset["destinationUrn"] for asset in properties.aspect.to_obj()["assets"]
    ] == list(resources)


def test_every_governed_dataset_has_schema_and_valid_primary_keys():
    products = (dp1(), dp2(), dp3(), cdc_ingestion(), streaming_features(), rag_items())
    datasets = [dataset for product in products for dataset in product.datasets]
    assert len(datasets) == 56
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
    assert fields["event_timestamp"]["type"] == {
        "type": {"com.linkedin.schema.TimeType": {}}
    }
    assert fields["source_run_id"]["isPartOfKey"] is True


def test_schema_contract_uses_native_datahub_graph_and_mcp():
    proposals = []

    class Graph:
        def upsert_custom_assertion(self, **kwargs):
            return {"urn": kwargs["urn"]}

        def execute_graphql(self, query, variables):
            return {"upsertDataContract": {"urn": "urn:li:dataContract:test"}}

    class Emitter:
        graph = Graph()

        def emit_mcp(self, proposal):
            proposals.append(proposal)

    dataset = next(
        item for item in dp1().datasets if item.name.endswith("bronze_behavior_events")
    )
    emit_dataset_contract(Emitter(), dataset)
    assertion = next(item for item in proposals if item.aspectName == "assertionInfo")
    info = assertion.aspect.to_obj()
    assert info["type"] == "DATA_SCHEMA"
    assert info["schemaAssertion"]["entity"] == dataset.urn
    assert info["schemaAssertion"]["compatibility"] == "EXACT_MATCH"


def test_dp3_silver_source_does_not_rebuild_silver(monkeypatch):
    import features.spark.dp3_offline_feature_entrypoint as dp3_entrypoint

    class Spark:
        def stop(self):
            pass

    silver = {"clean_behavior_events": object()}
    outputs = {"recsys_features.feature_store.user_sequence_features": object()}
    monkeypatch.setattr(dp3_entrypoint, "spark_session", lambda name: Spark())
    monkeypatch.setattr(
        dp3_entrypoint,
        "load_config",
        lambda path: {
            "input": {"source": "silver_lakehouse"},
            "output": {"feast_postgres_export": {"enabled": False}},
            "features": {},
        },
    )
    monkeypatch.setattr(dp3_entrypoint, "create_spark_namespace", lambda *args: None)
    monkeypatch.setattr(
        dp3_entrypoint, "read_silver_lakehouse_tables", lambda *args: silver
    )
    monkeypatch.setattr(
        dp3_entrypoint,
        "build_silver_tables",
        lambda *args, **kwargs: pytest.fail("DP3 rebuilt Silver"),
    )
    monkeypatch.setattr(
        dp3_entrypoint, "_build_feature_outputs", lambda *args, **kwargs: outputs
    )
    monkeypatch.setattr(
        dp3_entrypoint, "write_iceberg_table", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(
        dp3_entrypoint, "_write_postgres_tables", lambda *args, **kwargs: ()
    )
    monkeypatch.setattr(
        dp3_entrypoint,
        "_publish_dp3_iceberg_validation",
        lambda *args, **kwargs: {"status": "SUCCESS"},
    )
    monkeypatch.setattr(dp3_entrypoint, "row_count", lambda frame: 1)
    monkeypatch.setattr(
        dp3_entrypoint,
        "_output_summary",
        lambda frames: {"user_sequence_features": 1},
    )
    assert dp3_entrypoint.run_dp3_offline_features("unused.yaml") == {
        "clean_behavior_events": 1,
        "user_sequence_features": 1,
    }
