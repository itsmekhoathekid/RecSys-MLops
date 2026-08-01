from __future__ import annotations

import re

from lakehouse.iceberg import RAW_GENERATOR_TABLES, SILVER_LAKEHOUSE_TABLES


ENV = "PROD"
PIPELINE_FLOW_IDS = {
    "DP1": "recsys_dp1_raw_to_bronze",
    "DP2": "recsys_dp2_bronze_to_silver_gold",
    "DP3": "recsys_dp3_offline_feature_table",
    "CDC_INGESTION": "recsys_cdc_postgres_to_kafka",
    "STREAMING_FEATURES": "recsys_flink_stream_features",
    "ANALYTICS": "recsys_analytics_sync_silver",
}
FEATURE_TABLES = (
    "user_sequence_features",
    "user_aggregate_features",
    "item_features",
)
DP3_ICEBERG_TABLES = FEATURE_TABLES + ("ml_ranking_labels", "ml_bst_training")
DP3_POSTGRES_TABLES = FEATURE_TABLES + ("ml_ranking_labels",)


def dataset_urn(platform: str, name: str, env: str = ENV) -> str:
    return f"urn:li:dataset:(urn:li:dataPlatform:{platform},{name},{env})"


def flow_urn(flow_id: str, cluster: str = ENV) -> str:
    return f"urn:li:dataFlow:(airflow,{flow_id},{cluster})"


def pipeline_flow_id(pipeline: str) -> str:
    try:
        return PIPELINE_FLOW_IDS[pipeline]
    except KeyError as exc:
        raise ValueError(f"Unknown runtime-lineage pipeline: {pipeline}") from exc


def openlineage_job_name(pipeline: str, job_id: str) -> str:
    """Return the name DataHub's native OpenLineage mapper uses as the DataJob id."""
    return f"{pipeline_flow_id(pipeline)}.{job_id}"


def _flow_id_from_urn(flow: str) -> str:
    match = re.fullmatch(r"urn:li:dataFlow:\([^,]+,([^,]+),[^)]+\)", flow)
    if not match:
        raise ValueError(f"Invalid DataHub DataFlow URN: {flow}")
    return match.group(1)


def job_urn(flow: str, job_id: str) -> str:
    # The native OpenLineage converter derives the flow id from the prefix before the
    # first dot, but retains the full OpenLineage job name as the DataJob id.
    data_job_id = f"{_flow_id_from_urn(flow)}.{job_id}"
    return f"urn:li:dataJob:({flow},{data_job_id})"


def dataset_urn_parts(urn: str) -> tuple[str, str, str]:
    match = re.fullmatch(
        r"urn:li:dataset:\(urn:li:dataPlatform:([^,]+),(.+),([^,]+)\)", urn
    )
    if not match:
        raise ValueError(f"Invalid DataHub Dataset URN: {urn}")
    return match.group(1), match.group(2), match.group(3)


def bronze_urn(table: str) -> str:
    return dataset_urn("iceberg", f"recsys.lakehouse.bronze_{table}")


def silver_urn(table: str) -> str:
    return dataset_urn("iceberg", f"recsys.lakehouse.silver_{table}")


def iceberg_feature_urn(table: str) -> str:
    return dataset_urn("iceberg", f"recsys_features.feature_store.{table}")


def postgres_feature_urn(table: str) -> str:
    return dataset_urn("postgres", f"feature-postgres.feature_store.{table}")


def source_postgres_urn(table: str) -> str:
    return dataset_urn("postgres", f"source_postgres.recsys.public.{table}")


def kafka_topic_urn(table: str) -> str:
    return dataset_urn("kafka", f"recsys-dataflow.cdc.{table}")


def redis_feature_urn(table: str) -> str:
    return dataset_urn("redis", f"redis://redis.recsys-dataflow.svc.cluster.local:6379/{table}")


BRONZE_URNS = {table: bronze_urn(table) for table in RAW_GENERATOR_TABLES}
SILVER_URNS = {table: silver_urn(table) for table in SILVER_LAKEHOUSE_TABLES}
ICEBERG_FEATURE_URNS = {table: iceberg_feature_urn(table) for table in DP3_ICEBERG_TABLES}
POSTGRES_FEATURE_URNS = {table: postgres_feature_urn(table) for table in DP3_POSTGRES_TABLES}
SOURCE_POSTGRES_URNS = {table: source_postgres_urn(table) for table in RAW_GENERATOR_TABLES}
KAFKA_TOPIC_URNS = {table: kafka_topic_urn(table) for table in RAW_GENERATOR_TABLES}
REDIS_FEATURE_URNS = {table: redis_feature_urn(table) for table in FEATURE_TABLES}
