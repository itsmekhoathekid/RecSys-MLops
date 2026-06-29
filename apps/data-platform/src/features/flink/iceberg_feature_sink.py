from __future__ import annotations

from typing import Any

from lakehouse.iceberg import IcebergCatalogConfig, create_flink_catalog_sql


STREAM_TABLE_DDL = {
    "stream_behavior_events": """
CREATE TABLE IF NOT EXISTS {table_name} (
  event_id STRING,
  event_timestamp TIMESTAMP(3),
  processed_timestamp TIMESTAMP(3),
  user_id BIGINT,
  product_id BIGINT,
  event_type STRING,
  event_type_id INT,
  category_id INT,
  brand_id INT,
  price DOUBLE,
  price_bucket INT,
  payload_hash STRING,
  source_topic STRING,
  late_by_seconds DOUBLE,
  is_late BOOLEAN
)
""",
    "stream_user_sequence_features": """
CREATE TABLE IF NOT EXISTS {table_name} (
  user_id BIGINT,
  feature_timestamp TIMESTAMP(3),
  sequence_length INT,
  max_history_length INT,
  feature_payload STRING,
  feature_version STRING
)
""",
    "stream_user_aggregate_features": """
CREATE TABLE IF NOT EXISTS {table_name} (
  user_id BIGINT,
  feature_timestamp TIMESTAMP(3),
  views_30m INT,
  carts_30m INT,
  purchases_24h INT,
  feature_payload STRING,
  feature_version STRING
)
""",
    "stream_item_features": """
CREATE TABLE IF NOT EXISTS {table_name} (
  product_id BIGINT,
  feature_timestamp TIMESTAMP(3),
  category_id INT,
  brand_id INT,
  price_bucket INT,
  views_1h INT,
  views_24h INT,
  purchases_24h INT,
  popularity_score DOUBLE,
  feature_payload STRING,
  feature_version STRING
)
""",
    "streaming_quality_windows": """
CREATE TABLE IF NOT EXISTS {table_name} (
  window_start TIMESTAMP(3),
  window_end TIMESTAMP(3),
  topic STRING,
  event_count BIGINT,
  late_event_count BIGINT,
  duplicate_event_count BIGINT,
  max_late_by_seconds DOUBLE,
  is_bursty BOOLEAN,
  created_timestamp TIMESTAMP(3)
)
""",
}


def configure_iceberg_catalog(table_env: Any, config: IcebergCatalogConfig) -> None:
    table_env.execute_sql(
        create_flink_catalog_sql(
            config,
            catalog_name=config.offline_feature_catalog_name,
            warehouse_uri=config.offline_feature_warehouse_uri,
        )
    )
    table_env.execute_sql(f"USE CATALOG {config.offline_feature_catalog_name}")
    table_env.execute_sql(f"CREATE DATABASE IF NOT EXISTS {config.feature_namespace}")
    table_env.execute_sql(f"USE {config.feature_namespace}")
    for table_name, ddl in STREAM_TABLE_DDL.items():
        table_env.execute_sql(ddl.format(table_name=config.feature_table(table_name)))
