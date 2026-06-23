from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TableSpec:
    schema: str
    name: str
    columns: dict[str, str]
    primary_key: tuple[str, ...]

    @property
    def qualified_name(self) -> str:
        return f"{self.schema}.{self.name}"


STAGING_STREAM_BEHAVIOR_EVENTS = TableSpec(
    schema="staging",
    name="stream_behavior_events",
    columns={
        "event_id": "TEXT",
        "event_timestamp": "TIMESTAMPTZ",
        "processed_timestamp": "TIMESTAMPTZ",
        "user_id": "BIGINT",
        "product_id": "BIGINT",
        "event_type": "TEXT",
        "event_type_id": "SMALLINT",
        "category_id": "BIGINT",
        "brand_id": "BIGINT",
        "price": "DOUBLE PRECISION",
        "price_bucket": "SMALLINT",
        "payload_hash": "TEXT",
        "source_topic": "TEXT",
        "late_by_seconds": "DOUBLE PRECISION",
        "is_late": "BOOLEAN",
    },
    primary_key=("event_id",),
)

STAGING_STREAM_USER_SEQUENCE_FEATURES = TableSpec(
    schema="staging",
    name="stream_user_sequence_features",
    columns={
        "user_id": "BIGINT",
        "feature_timestamp": "TIMESTAMPTZ",
        "sequence_length": "INTEGER",
        "max_history_length": "INTEGER",
        "feature_payload": "JSONB",
        "feature_version": "TEXT",
    },
    primary_key=("user_id", "feature_timestamp"),
)

STAGING_STREAM_USER_AGGREGATE_FEATURES = TableSpec(
    schema="staging",
    name="stream_user_aggregate_features",
    columns={
        "user_id": "BIGINT",
        "feature_timestamp": "TIMESTAMPTZ",
        "views_30m": "INTEGER",
        "carts_30m": "INTEGER",
        "purchases_24h": "INTEGER",
        "feature_payload": "JSONB",
        "feature_version": "TEXT",
    },
    primary_key=("user_id", "feature_timestamp"),
)

STAGING_STREAM_ITEM_FEATURES = TableSpec(
    schema="staging",
    name="stream_item_features",
    columns={
        "product_id": "BIGINT",
        "feature_timestamp": "TIMESTAMPTZ",
        "category_id": "BIGINT",
        "brand_id": "BIGINT",
        "price_bucket": "SMALLINT",
        "views_1h": "INTEGER",
        "views_24h": "INTEGER",
        "purchases_24h": "INTEGER",
        "popularity_score": "DOUBLE PRECISION",
        "feature_payload": "JSONB",
        "feature_version": "TEXT",
    },
    primary_key=("product_id", "feature_timestamp"),
)

MONITORING_STREAMING_QUALITY_WINDOWS = TableSpec(
    schema="monitoring",
    name="streaming_quality_windows",
    columns={
        "window_start": "TIMESTAMPTZ",
        "window_end": "TIMESTAMPTZ",
        "topic": "TEXT",
        "event_count": "INTEGER",
        "late_event_count": "INTEGER",
        "max_late_by_seconds": "DOUBLE PRECISION",
        "is_bursty": "BOOLEAN",
        "created_timestamp": "TIMESTAMPTZ",
    },
    primary_key=("window_start", "window_end", "topic"),
)

MONITORING_DATA_QUALITY_RUNS = TableSpec(
    schema="monitoring",
    name="data_quality_runs",
    columns={
        "run_id": "TEXT",
        "check_name": "TEXT",
        "passed": "BOOLEAN",
        "error_count": "INTEGER",
        "metrics": "JSONB",
        "created_timestamp": "TIMESTAMPTZ",
    },
    primary_key=("run_id", "check_name"),
)

MONITORING_FEATURE_DRIFT_RUNS = TableSpec(
    schema="monitoring",
    name="feature_drift_runs",
    columns={
        "run_id": "TEXT",
        "feature_name": "TEXT",
        "drift_score": "DOUBLE PRECISION",
        "passed": "BOOLEAN",
        "metrics": "JSONB",
        "created_timestamp": "TIMESTAMPTZ",
    },
    primary_key=("run_id", "feature_name"),
)

MONITORING_ONLINE_STORE_SYNC_RUNS = TableSpec(
    schema="monitoring",
    name="online_store_sync_runs",
    columns={
        "run_id": "TEXT",
        "feature_view": "TEXT",
        "scanned_rows": "INTEGER",
        "synced_rows": "INTEGER",
        "skipped_rows": "INTEGER",
        "created_timestamp": "TIMESTAMPTZ",
    },
    primary_key=("run_id", "feature_view"),
)

WAREHOUSE_TABLES = [
    STAGING_STREAM_BEHAVIOR_EVENTS,
    STAGING_STREAM_USER_SEQUENCE_FEATURES,
    STAGING_STREAM_USER_AGGREGATE_FEATURES,
    STAGING_STREAM_ITEM_FEATURES,
    MONITORING_STREAMING_QUALITY_WINDOWS,
    MONITORING_DATA_QUALITY_RUNS,
    MONITORING_FEATURE_DRIFT_RUNS,
    MONITORING_ONLINE_STORE_SYNC_RUNS,
]
