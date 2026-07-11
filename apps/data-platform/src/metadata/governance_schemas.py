from __future__ import annotations

from dataclasses import dataclass

from feature_store.postgres_offline_store import TABLE_SCHEMAS as POSTGRES_TABLE_SCHEMAS
from ingest.postgres_cdc_contracts import primary_keys_by_table


@dataclass(frozen=True)
class SchemaColumn:
    name: str
    native_type: str
    description: str = ""
    nullable: bool = True


def _columns(*items: tuple[str, str]) -> tuple[SchemaColumn, ...]:
    return tuple(SchemaColumn(name, native_type) for name, native_type in items)


RAW_TABLE_SCHEMAS: dict[str, tuple[SchemaColumn, ...]] = {
    "users": _columns(
        ("user_id", "BIGINT"),
        ("signup_ts", "TIMESTAMP<US,UTC>"),
        ("signup_channel", "STRING"),
        ("city", "STRING"),
        ("country", "STRING"),
        ("segment", "STRING"),
        ("age_bucket", "SMALLINT"),
        ("preferred_category_id", "BIGINT"),
        ("preferred_brand_id", "BIGINT"),
        ("price_sensitivity", "DOUBLE"),
        ("user_lifecycle_state", "STRING"),
        ("last_active_ts", "TIMESTAMP<US,UTC>"),
        ("is_active", "BOOLEAN"),
        ("created_ts", "TIMESTAMP<US,UTC>"),
        ("updated_ts", "TIMESTAMP<US,UTC>"),
    ),
    "user_preferences": _columns(
        ("user_id", "BIGINT"),
        ("category_id", "BIGINT"),
        ("brand_id", "BIGINT"),
        ("preference_weight", "DOUBLE"),
        ("source", "STRING"),
        ("created_ts", "TIMESTAMP<US,UTC>"),
        ("updated_ts", "TIMESTAMP<US,UTC>"),
    ),
    "products": _columns(
        ("product_id", "BIGINT"),
        ("product_name", "STRING"),
        ("category_id", "BIGINT"),
        ("category_code", "STRING"),
        ("brand_id", "BIGINT"),
        ("brand_name", "STRING"),
        ("base_price", "DECIMAL(18,2)"),
        ("current_price", "DECIMAL(18,2)"),
        ("price_bucket", "SMALLINT"),
        ("popularity_weight", "DOUBLE"),
        ("is_active", "BOOLEAN"),
        ("created_ts", "TIMESTAMP<US,UTC>"),
        ("updated_ts", "TIMESTAMP<US,UTC>"),
    ),
    "product_snapshots": _columns(
        ("product_id", "BIGINT"),
        ("valid_from", "TIMESTAMP<US,UTC>"),
        ("valid_to", "TIMESTAMP<US,UTC>"),
        ("category_id", "BIGINT"),
        ("category_code", "STRING"),
        ("brand_id", "BIGINT"),
        ("brand_name", "STRING"),
        ("current_price", "DECIMAL(18,2)"),
        ("price_bucket", "SMALLINT"),
        ("is_active", "BOOLEAN"),
        ("created_ts", "TIMESTAMP<US,UTC>"),
    ),
    "sessions": _columns(
        ("session_id", "STRING"),
        ("user_id", "BIGINT"),
        ("session_start_ts", "TIMESTAMP<US,UTC>"),
        ("session_end_ts", "TIMESTAMP<US,UTC>"),
        ("entry_source", "STRING"),
        ("device_type", "STRING"),
        ("campaign_id", "STRING"),
        ("session_end_reason", "STRING"),
        ("created_ts", "TIMESTAMP<US,UTC>"),
        ("updated_ts", "TIMESTAMP<US,UTC>"),
    ),
    "recommendation_requests": _columns(
        ("request_id", "STRING"),
        ("user_id", "BIGINT"),
        ("session_id", "STRING"),
        ("request_timestamp", "TIMESTAMP<US,UTC>"),
        ("surface", "STRING"),
        ("context_product_id", "BIGINT"),
        ("context_category_id", "BIGINT"),
        ("device_type", "STRING"),
        ("source", "STRING"),
        ("campaign_id", "STRING"),
        ("created_ts", "TIMESTAMP<US,UTC>"),
        ("schema_version", "SMALLINT"),
    ),
    "impressions": _columns(
        ("impression_id", "STRING"),
        ("request_id", "STRING"),
        ("user_id", "BIGINT"),
        ("session_id", "STRING"),
        ("impression_timestamp", "TIMESTAMP<US,UTC>"),
        ("candidate_product_id", "BIGINT"),
        ("rank_position", "INTEGER"),
        ("candidate_source", "STRING"),
        ("retrieval_score", "DOUBLE"),
        ("ranking_score", "DOUBLE"),
        ("surface", "STRING"),
        ("is_clicked", "BOOLEAN"),
        ("created_ts", "TIMESTAMP<US,UTC>"),
        ("schema_version", "SMALLINT"),
    ),
    "behavior_events": _columns(
        ("event_id", "STRING"),
        ("event_timestamp", "TIMESTAMP<US,UTC>"),
        ("created_ts", "TIMESTAMP<US,UTC>"),
        ("ingestion_ts", "TIMESTAMP<US,UTC>"),
        ("user_id", "BIGINT"),
        ("session_id", "STRING"),
        ("request_id", "STRING"),
        ("impression_id", "STRING"),
        ("event_type", "STRING"),
        ("product_id", "BIGINT"),
        ("category_id", "BIGINT"),
        ("brand_id", "BIGINT"),
        ("price", "DECIMAL(18,2)"),
        ("price_bucket", "SMALLINT"),
        ("quantity", "INTEGER"),
        ("device_type", "STRING"),
        ("source", "STRING"),
        ("campaign_id", "STRING"),
        ("page_context", "STRING"),
        ("rank_position", "INTEGER"),
        ("order_id", "STRING"),
        ("payload_hash", "STRING"),
        ("event_date", "DATE"),
        ("schema_version", "SMALLINT"),
        ("drift_enabled", "BOOLEAN"),
        ("drift_scenario", "STRING"),
        ("drift_phase", "STRING"),
        ("drift_factor", "DOUBLE"),
    ),
    "orders": _columns(
        ("order_id", "STRING"),
        ("user_id", "BIGINT"),
        ("session_id", "STRING"),
        ("order_timestamp", "TIMESTAMP<US,UTC>"),
        ("status", "STRING"),
        ("gross_amount", "DECIMAL(18,2)"),
        ("discount_amount", "DECIMAL(18,2)"),
        ("net_amount", "DECIMAL(18,2)"),
        ("coupon_code", "STRING"),
        ("payment_method", "STRING"),
        ("shipping_city", "STRING"),
        ("paid_ts", "TIMESTAMP<US,UTC>"),
        ("cancelled_ts", "TIMESTAMP<US,UTC>"),
        ("refunded_ts", "TIMESTAMP<US,UTC>"),
        ("created_ts", "TIMESTAMP<US,UTC>"),
        ("updated_ts", "TIMESTAMP<US,UTC>"),
        ("drift_enabled", "BOOLEAN"),
        ("drift_scenario", "STRING"),
        ("drift_phase", "STRING"),
        ("drift_factor", "DOUBLE"),
    ),
    "order_items": _columns(
        ("order_item_id", "STRING"),
        ("order_id", "STRING"),
        ("product_id", "BIGINT"),
        ("quantity", "INTEGER"),
        ("unit_price", "DECIMAL(18,2)"),
        ("discount_amount", "DECIMAL(18,2)"),
        ("line_amount", "DECIMAL(18,2)"),
        ("created_ts", "TIMESTAMP<US,UTC>"),
    ),
}

BRONZE_AUDIT_COLUMNS = _columns(
    ("source_run_id", "STRING"),
    ("lakehouse_ingestion_ts", "TIMESTAMP<US,UTC>"),
)


def _merge_columns(*groups: tuple[SchemaColumn, ...]) -> tuple[SchemaColumn, ...]:
    merged: dict[str, SchemaColumn] = {}
    for group in groups:
        for column in group:
            merged.setdefault(column.name, column)
    return tuple(merged.values())


def raw_schema(table: str) -> tuple[SchemaColumn, ...]:
    return RAW_TABLE_SCHEMAS[table]


def bronze_schema(table: str) -> tuple[SchemaColumn, ...]:
    return _merge_columns(raw_schema(table), BRONZE_AUDIT_COLUMNS)


SILVER_TABLE_SCHEMAS: dict[str, tuple[SchemaColumn, ...]] = {
    "clean_behavior_events": _merge_columns(
        bronze_schema("behavior_events"),
        _columns(("event_type_id", "SMALLINT")),
    ),
    "rejected_behavior_events": _merge_columns(
        bronze_schema("behavior_events"),
        _columns(("event_type_id", "SMALLINT")),
    ),
    "clean_impressions": bronze_schema("impressions"),
    "clean_recommendation_requests": _merge_columns(
        bronze_schema("recommendation_requests"),
        _columns(("request_context", "STRING")),
    ),
    "order_facts": _merge_columns(
        bronze_schema("order_items"),
        bronze_schema("orders"),
        _columns(("is_valid_purchase", "BOOLEAN")),
    ),
    "product_scd": bronze_schema("product_snapshots"),
    "users": bronze_schema("users"),
    "products": bronze_schema("products"),
    "user_preferences": bronze_schema("user_preferences"),
}


def silver_schema(table: str) -> tuple[SchemaColumn, ...]:
    return SILVER_TABLE_SCHEMAS[table]


FEATURE_TABLE_SCHEMAS: dict[str, tuple[SchemaColumn, ...]] = {
    table: _columns(*tuple((name, native_type) for name, native_type in schema))
    for table, schema in POSTGRES_TABLE_SCHEMAS.items()
    if table in {"user_sequence_features", "user_aggregate_features", "item_features", "ml_ranking_labels"}
}
FEATURE_TABLE_SCHEMAS["ml_bst_training"] = _columns(
    ("impression_id", "STRING"),
    ("request_id", "STRING"),
    ("user_id", "INTEGER"),
    ("hist_item_id", "ARRAY<INTEGER>"),
    ("hist_event_type", "ARRAY<INTEGER>"),
    ("hist_category", "ARRAY<INTEGER>"),
    ("hist_brand", "ARRAY<INTEGER>"),
    ("hist_price_bucket", "ARRAY<INTEGER>"),
    ("hist_time", "ARRAY<INTEGER>"),
    ("target_item_id", "INTEGER"),
    ("target_category", "INTEGER"),
    ("target_brand", "INTEGER"),
    ("target_price_bucket", "INTEGER"),
    ("event_time", "BIGINT"),
    ("prediction_timestamp", "TIMESTAMP"),
    ("label", "INTEGER"),
    ("views_30m", "INTEGER"),
    ("carts_30m", "INTEGER"),
    ("purchases_24h", "INTEGER"),
    ("max_history_length", "INTEGER"),
)


def feature_schema(table: str) -> tuple[SchemaColumn, ...]:
    return FEATURE_TABLE_SCHEMAS[table]


def cdc_topic_schema(table: str) -> tuple[SchemaColumn, ...]:
    payload = tuple(
        SchemaColumn(
            name=f"payload.after.{column.name}",
            native_type=column.native_type,
            description=f"Debezium after-image field {column.name}.",
            nullable=True,
        )
        for column in raw_schema(table)
    )
    return _columns(
        ("payload.op", "STRING"),
        ("payload.ts_ms", "BIGINT"),
        ("payload.source", "RECORD"),
        ("payload.before", "RECORD"),
        ("payload.after", "RECORD"),
        ("payload.transaction", "RECORD"),
    ) + payload


RAW_PRIMARY_KEYS = primary_keys_by_table()

SILVER_PRIMARY_KEYS: dict[str, tuple[str, ...]] = {
    "clean_behavior_events": ("event_id",),
    "rejected_behavior_events": (),
    "clean_impressions": ("impression_id",),
    "clean_recommendation_requests": ("request_id",),
    "order_facts": ("order_item_id",),
    "product_scd": ("product_id", "valid_from"),
    "users": ("user_id",),
    "products": ("product_id",),
    "user_preferences": ("user_id", "category_id", "brand_id"),
}

FEATURE_PRIMARY_KEYS: dict[str, tuple[str, ...]] = {
    "user_sequence_features": ("user_id", "feature_timestamp"),
    "user_aggregate_features": ("user_id", "feature_timestamp"),
    "item_features": ("product_id", "feature_timestamp"),
    "ml_ranking_labels": ("impression_id", "candidate_product_id"),
    "ml_bst_training": ("impression_id", "target_item_id"),
}
