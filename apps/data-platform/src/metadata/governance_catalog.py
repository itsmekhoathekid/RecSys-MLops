from __future__ import annotations

from lakehouse.iceberg import RAW_GENERATOR_TABLES, SILVER_LAKEHOUSE_TABLES


ENV = "PROD"
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


def job_urn(flow: str, job_id: str) -> str:
    return f"urn:li:dataJob:({flow},{job_id})"


def bronze_urn(table: str) -> str:
    return dataset_urn("parquet", f"recsys.lakehouse.bronze_{table}")


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
