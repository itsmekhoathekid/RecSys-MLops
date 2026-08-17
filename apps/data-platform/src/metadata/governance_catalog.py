from __future__ import annotations

import re
import uuid

from lakehouse.iceberg import RAW_GENERATOR_TABLES, SILVER_LAKEHOUSE_TABLES


ENV = "PROD"
ASSERTION_NAMESPACE = uuid.UUID("5851f697-2fcb-4938-b5c8-34fcb1f9f297")
PIPELINE_FLOW_IDS = {
    "DP1": "recsys_dp1_raw_to_bronze",
    "DP2": "recsys_dp2_bronze_to_silver_gold",
    "DP3": "recsys_dp3_offline_feature_table",
    "CDC_INGESTION": "recsys_cdc_postgres_to_kafka",
    "STREAMING_FEATURES": "recsys_flink_stream_features",
    "ANALYTICS": "recsys_analytics_sync_silver",
    "RAG_ITEMS": "recsys_rag_item_index",
}
PIPELINE_ORCHESTRATORS = {
    "DP1": "airflow",
    "DP2": "airflow",
    "DP3": "airflow",
    "CDC_INGESTION": "kafka-connect",
    "STREAMING_FEATURES": "flink",
    "ANALYTICS": "airflow",
    "RAG_ITEMS": "airflow",
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


def assertion_urn(dataset: str, assertion_type: str = "quality") -> str:
    identity = uuid.uuid5(ASSERTION_NAMESPACE, f"{dataset}:{assertion_type}")
    return f"urn:li:assertion:{identity}"


def data_contract_id(dataset: str) -> str:
    value = f"{dataset}-contract"
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-").lower()
    return slug[:180] or "recsys-data-contract"


def flow_urn(
    flow_id: str,
    cluster: str = ENV,
    *,
    orchestrator: str = "airflow",
) -> str:
    return f"urn:li:dataFlow:({orchestrator},{flow_id},{cluster})"


def pipeline_flow_id(pipeline: str) -> str:
    try:
        return PIPELINE_FLOW_IDS[pipeline]
    except KeyError as exc:
        raise ValueError(f"Unknown runtime-lineage pipeline: {pipeline}") from exc


def pipeline_orchestrator(pipeline: str) -> str:
    try:
        return PIPELINE_ORCHESTRATORS[pipeline]
    except KeyError as exc:
        raise ValueError(f"Unknown runtime-lineage pipeline: {pipeline}") from exc


def pipeline_flow_urn(pipeline: str, cluster: str = ENV) -> str:
    return flow_urn(
        pipeline_flow_id(pipeline),
        cluster,
        orchestrator=pipeline_orchestrator(pipeline),
    )


def job_urn(flow: str, job_id: str) -> str:
    if not re.fullmatch(r"urn:li:dataFlow:\([^,]+,[^,]+,[^)]+\)", flow):
        raise ValueError(f"Invalid DataHub DataFlow URN: {flow}")
    return f"urn:li:dataJob:({flow},{job_id})"


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
    return dataset_urn(
        "redis", f"redis://redis.recsys-dataflow.svc.cluster.local:6379/{table}"
    )


def rag_s3_urn(path: str) -> str:
    """Return the run-agnostic governed dataset identity for one RAG artifact."""

    return dataset_urn("s3", f"recsys-lakehouse/{path}")


RAG_SOURCE_PRODUCTS_URN = source_postgres_urn("products")
RAG_RAW_DOCUMENTS_URN = rag_s3_urn("raw/rag_item_documents")
RAG_SILVER_CHUNKS_URN = rag_s3_urn("silver/rag_item_chunks")
RAG_GOLD_EMBEDDINGS_URN = rag_s3_urn("gold/rag_item_embeddings")
RAG_ACTIVE_POINTER_URN = rag_s3_urn("gold/rag_item_embeddings/_active")
RAG_MILVUS_URNS = {
    slot: dataset_urn("milvus", f"recsys_rag.rag_item_chunks_{slot}")
    for slot in ("blue", "green")
}


BRONZE_URNS = {table: bronze_urn(table) for table in RAW_GENERATOR_TABLES}
SILVER_URNS = {table: silver_urn(table) for table in SILVER_LAKEHOUSE_TABLES}
ICEBERG_FEATURE_URNS = {
    table: iceberg_feature_urn(table) for table in DP3_ICEBERG_TABLES
}
POSTGRES_FEATURE_URNS = {
    table: postgres_feature_urn(table) for table in DP3_POSTGRES_TABLES
}
SOURCE_POSTGRES_URNS = {
    table: source_postgres_urn(table) for table in RAW_GENERATOR_TABLES
}
KAFKA_TOPIC_URNS = {table: kafka_topic_urn(table) for table in RAW_GENERATOR_TABLES}
REDIS_FEATURE_URNS = {table: redis_feature_urn(table) for table in FEATURE_TABLES}
