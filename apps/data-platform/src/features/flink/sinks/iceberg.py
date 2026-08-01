from __future__ import annotations

from typing import Any

from feature_store.online_writer import dumps_feature_payload
from features.flink.operators.row_mappers import (
    build_late_event_dlq_row,
    build_offline_item_feature_rows,
    build_offline_user_feature_rows,
    build_stream_behavior_row,
    flink_timestamp,
)
from features.flink.pyflink_compat import FilterFunction, MapFunction
from lakehouse.iceberg import (
    IcebergCatalogConfig,
    create_flink_catalog_sql,
)


STREAM_TABLE_DDL = {
    "stream_behavior_events": """
CREATE TABLE IF NOT EXISTS {table_name} (
  event_id STRING, event_timestamp TIMESTAMP(3), processed_timestamp TIMESTAMP(3),
  user_id BIGINT, product_id BIGINT, event_type STRING, event_type_id INT,
  category_id INT, brand_id INT, price DOUBLE, price_bucket INT,
  payload_hash STRING, source_topic STRING, late_by_seconds DOUBLE, is_late BOOLEAN
)
""",
    "stream_user_sequence_features": """
CREATE TABLE IF NOT EXISTS {table_name} (
  user_id BIGINT, feature_timestamp TIMESTAMP(3), sequence_length INT,
  max_history_length INT, feature_payload STRING, feature_version STRING
)
""",
    "stream_user_aggregate_features": """
CREATE TABLE IF NOT EXISTS {table_name} (
  user_id BIGINT, feature_timestamp TIMESTAMP(3), views_30m INT, carts_30m INT,
  purchases_24h INT, feature_payload STRING, feature_version STRING
)
""",
    "stream_item_features": """
CREATE TABLE IF NOT EXISTS {table_name} (
  product_id BIGINT, feature_timestamp TIMESTAMP(3), category_id INT, brand_id INT,
  price_bucket INT, views_1h INT, views_24h INT, purchases_24h INT,
  popularity_score DOUBLE, feature_payload STRING, feature_version STRING
)
""",
    "streaming_quality_windows": """
CREATE TABLE IF NOT EXISTS {table_name} (
  window_start TIMESTAMP(3), window_end TIMESTAMP(3), topic STRING,
  event_count BIGINT, late_event_count BIGINT, late_events_dropped BIGINT,
  side_output_late_events BIGINT, duplicate_event_count BIGINT,
  max_late_by_seconds DOUBLE, is_bursty BOOLEAN, created_timestamp TIMESTAMP(3)
)
""",
    "stream_late_events_dlq": """
CREATE TABLE IF NOT EXISTS {table_name} (
  event_id STRING, user_id BIGINT, product_id BIGINT, event_type STRING,
  event_timestamp TIMESTAMP(3), processed_timestamp TIMESTAMP(3),
  late_by_seconds DOUBLE, allowed_lateness_seconds BIGINT, source_topic STRING,
  payload_hash STRING, reason STRING, payload STRING, created_timestamp TIMESTAMP(3)
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


def _flink_row(*values: Any):
    from pyflink.common import Row

    return Row(*values)


class KeepRows(FilterFunction):
    def filter(self, value: Any | None) -> bool:
        return value is not None


class StreamBehaviorEventRow(MapFunction):
    def __init__(self, args: Any) -> None:
        self.args = args

    def map(self, event: dict[str, Any]):
        row = build_stream_behavior_row(
            event, self.args.topic, self.args.allowed_lateness_seconds
        )
        return _flink_row(
            row["event_id"],
            flink_timestamp(row["event_timestamp"]),
            flink_timestamp(row["processed_timestamp"]),
            row["user_id"],
            row["product_id"],
            row["event_type"],
            row["event_type_id"],
            row["category_id"],
            row["brand_id"],
            row["price"],
            row["price_bucket"],
            row["payload_hash"],
            row["source_topic"],
            row["late_by_seconds"],
            row["is_late"],
        )


class UserSequenceFeatureRow(MapFunction):
    def map(self, update: dict[str, Any]):
        row = build_offline_user_feature_rows(update)["stream_user_sequence_features"][
            0
        ]
        return _flink_row(
            row["user_id"],
            flink_timestamp(row["feature_timestamp"]),
            row["sequence_length"],
            row["max_history_length"],
            dumps_feature_payload(row["feature_payload"]),
            row["feature_version"],
        )


class UserAggregateFeatureRow(MapFunction):
    def map(self, update: dict[str, Any]):
        row = build_offline_user_feature_rows(update)["stream_user_aggregate_features"][
            0
        ]
        return _flink_row(
            row["user_id"],
            flink_timestamp(row["feature_timestamp"]),
            row["views_30m"],
            row["carts_30m"],
            row["purchases_24h"],
            dumps_feature_payload(row["feature_payload"]),
            row["feature_version"],
        )


class ItemFeatureRow(MapFunction):
    def map(self, update: dict[str, Any]):
        row = build_offline_item_feature_rows(update)["stream_item_features"][0]
        return _flink_row(
            row["product_id"],
            flink_timestamp(row["feature_timestamp"]),
            row["category_id"],
            row["brand_id"],
            row["price_bucket"],
            row["views_1h"],
            row["views_24h"],
            row["purchases_24h"],
            row["popularity_score"],
            dumps_feature_payload(row["feature_payload"]),
            row["feature_version"],
        )


class QualityWindowRow(MapFunction):
    def map(self, row: dict[str, Any]):
        return _flink_row(
            flink_timestamp(row["window_start"]),
            flink_timestamp(row["window_end"]),
            row["topic"],
            row["event_count"],
            row["late_event_count"],
            row["late_events_dropped"],
            row["side_output_late_events"],
            row["duplicate_event_count"],
            row["max_late_by_seconds"],
            row["is_bursty"],
            flink_timestamp(row["created_timestamp"]),
        )


class LateEventDlqRow(MapFunction):
    def __init__(self, args: Any) -> None:
        self.args = args

    def map(self, event: dict[str, Any]):
        row = build_late_event_dlq_row(
            event, self.args.topic, self.args.allowed_lateness_seconds
        )
        return _flink_row(
            row["event_id"],
            row["user_id"],
            row["product_id"],
            row["event_type"],
            flink_timestamp(row["event_timestamp"]),
            flink_timestamp(row["processed_timestamp"]),
            row["late_by_seconds"],
            row["allowed_lateness_seconds"],
            row["source_topic"],
            row["payload_hash"],
            row["reason"],
            row["payload"],
            flink_timestamp(row["created_timestamp"]),
        )


def build_iceberg_statement_set(
    env: Any,
    args: Any,
    *,
    feature_events: Any,
    user_updates: Any,
    item_updates: Any,
    quality_rows: Any,
    late_events: Any,
):
    from pyflink.common import Types
    from pyflink.table import StreamTableEnvironment

    catalog = IcebergCatalogConfig(
        catalog_name=args.iceberg_catalog,
        offline_feature_catalog_name=args.offline_feature_catalog,
        feature_namespace=args.iceberg_feature_namespace,
        warehouse_uri=args.lakehouse_warehouse,
        offline_feature_warehouse_uri=args.offline_feature_store_warehouse,
    )
    table_env = StreamTableEnvironment.create(env)
    configure_iceberg_catalog(table_env, catalog)
    statement_set = table_env.create_statement_set()

    def add_insert(name: str, stream: Any) -> None:
        table = table_env.from_data_stream(stream)
        statement_set.add_insert(catalog.feature_table(name), table)

    behavior_stream = feature_events.map(
        StreamBehaviorEventRow(args),
        output_type=Types.ROW_NAMED(
            [
                "event_id",
                "event_timestamp",
                "processed_timestamp",
                "user_id",
                "product_id",
                "event_type",
                "event_type_id",
                "category_id",
                "brand_id",
                "price",
                "price_bucket",
                "payload_hash",
                "source_topic",
                "late_by_seconds",
                "is_late",
            ],
            [
                Types.STRING(),
                Types.SQL_TIMESTAMP(),
                Types.SQL_TIMESTAMP(),
                Types.LONG(),
                Types.LONG(),
                Types.STRING(),
                Types.INT(),
                Types.INT(),
                Types.INT(),
                Types.DOUBLE(),
                Types.INT(),
                Types.STRING(),
                Types.STRING(),
                Types.DOUBLE(),
                Types.BOOLEAN(),
            ],
        ),
    ).filter(KeepRows())
    add_insert("stream_behavior_events", behavior_stream)
    add_insert(
        "stream_user_sequence_features",
        user_updates.map(
            UserSequenceFeatureRow(),
            output_type=Types.ROW_NAMED(
                [
                    "user_id",
                    "feature_timestamp",
                    "sequence_length",
                    "max_history_length",
                    "feature_payload",
                    "feature_version",
                ],
                [
                    Types.LONG(),
                    Types.SQL_TIMESTAMP(),
                    Types.INT(),
                    Types.INT(),
                    Types.STRING(),
                    Types.STRING(),
                ],
            ),
        ).filter(KeepRows()),
    )
    add_insert(
        "stream_user_aggregate_features",
        user_updates.map(
            UserAggregateFeatureRow(),
            output_type=Types.ROW_NAMED(
                [
                    "user_id",
                    "feature_timestamp",
                    "views_30m",
                    "carts_30m",
                    "purchases_24h",
                    "feature_payload",
                    "feature_version",
                ],
                [
                    Types.LONG(),
                    Types.SQL_TIMESTAMP(),
                    Types.INT(),
                    Types.INT(),
                    Types.INT(),
                    Types.STRING(),
                    Types.STRING(),
                ],
            ),
        ).filter(KeepRows()),
    )
    add_insert(
        "stream_item_features",
        item_updates.map(
            ItemFeatureRow(),
            output_type=Types.ROW_NAMED(
                [
                    "product_id",
                    "feature_timestamp",
                    "category_id",
                    "brand_id",
                    "price_bucket",
                    "views_1h",
                    "views_24h",
                    "purchases_24h",
                    "popularity_score",
                    "feature_payload",
                    "feature_version",
                ],
                [
                    Types.LONG(),
                    Types.SQL_TIMESTAMP(),
                    Types.INT(),
                    Types.INT(),
                    Types.INT(),
                    Types.INT(),
                    Types.INT(),
                    Types.INT(),
                    Types.DOUBLE(),
                    Types.STRING(),
                    Types.STRING(),
                ],
            ),
        ).filter(KeepRows()),
    )
    add_insert(
        "streaming_quality_windows",
        quality_rows.map(
            QualityWindowRow(),
            output_type=Types.ROW_NAMED(
                [
                    "window_start",
                    "window_end",
                    "topic",
                    "event_count",
                    "late_event_count",
                    "late_events_dropped",
                    "side_output_late_events",
                    "duplicate_event_count",
                    "max_late_by_seconds",
                    "is_bursty",
                    "created_timestamp",
                ],
                [
                    Types.SQL_TIMESTAMP(),
                    Types.SQL_TIMESTAMP(),
                    Types.STRING(),
                    Types.LONG(),
                    Types.LONG(),
                    Types.LONG(),
                    Types.LONG(),
                    Types.LONG(),
                    Types.DOUBLE(),
                    Types.BOOLEAN(),
                    Types.SQL_TIMESTAMP(),
                ],
            ),
        ),
    )
    add_insert(
        "stream_late_events_dlq",
        late_events.map(
            LateEventDlqRow(args),
            output_type=Types.ROW_NAMED(
                [
                    "event_id",
                    "user_id",
                    "product_id",
                    "event_type",
                    "event_timestamp",
                    "processed_timestamp",
                    "late_by_seconds",
                    "allowed_lateness_seconds",
                    "source_topic",
                    "payload_hash",
                    "reason",
                    "payload",
                    "created_timestamp",
                ],
                [
                    Types.STRING(),
                    Types.LONG(),
                    Types.LONG(),
                    Types.STRING(),
                    Types.SQL_TIMESTAMP(),
                    Types.SQL_TIMESTAMP(),
                    Types.DOUBLE(),
                    Types.LONG(),
                    Types.STRING(),
                    Types.STRING(),
                    Types.STRING(),
                    Types.STRING(),
                    Types.SQL_TIMESTAMP(),
                ],
            ),
        ),
    )
    return statement_set
