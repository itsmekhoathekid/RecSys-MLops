from __future__ import annotations

import pyarrow as pa


TS = pa.timestamp("us", tz="UTC")
MONEY = pa.decimal128(18, 2)
UUID = pa.string()


SCHEMAS: dict[str, pa.Schema] = {
    "users": pa.schema(
        [
            ("user_id", pa.int64()),
            ("signup_ts", TS),
            ("signup_channel", pa.string()),
            ("city", pa.string()),
            ("country", pa.string()),
            ("segment", pa.string()),
            ("age_bucket", pa.int16()),
            ("preferred_category_id", pa.int64()),
            ("preferred_brand_id", pa.int64()),
            ("price_sensitivity", pa.float64()),
            ("user_lifecycle_state", pa.string()),
            ("last_active_ts", TS),
            ("is_active", pa.bool_()),
            ("created_ts", TS),
            ("updated_ts", TS),
        ]
    ),
    "user_preferences": pa.schema(
        [
            ("user_id", pa.int64()),
            ("category_id", pa.int64()),
            ("brand_id", pa.int64()),
            ("preference_weight", pa.float64()),
            ("source", pa.string()),
            ("created_ts", TS),
            ("updated_ts", TS),
        ]
    ),
    "products": pa.schema(
        [
            ("product_id", pa.int64()),
            ("product_name", pa.string()),
            ("category_id", pa.int64()),
            ("category_code", pa.string()),
            ("brand_id", pa.int64()),
            ("brand_name", pa.string()),
            ("base_price", MONEY),
            ("current_price", MONEY),
            ("price_bucket", pa.int16()),
            ("popularity_weight", pa.float64()),
            ("is_active", pa.bool_()),
            ("created_ts", TS),
            ("updated_ts", TS),
        ]
    ),
    "product_snapshots": pa.schema(
        [
            ("product_id", pa.int64()),
            ("valid_from", TS),
            ("valid_to", TS),
            ("category_id", pa.int64()),
            ("category_code", pa.string()),
            ("brand_id", pa.int64()),
            ("brand_name", pa.string()),
            ("current_price", MONEY),
            ("price_bucket", pa.int16()),
            ("is_active", pa.bool_()),
            ("created_ts", TS),
        ]
    ),
    "sessions": pa.schema(
        [
            ("session_id", UUID),
            ("user_id", pa.int64()),
            ("session_start_ts", TS),
            ("session_end_ts", TS),
            ("entry_source", pa.string()),
            ("device_type", pa.string()),
            ("campaign_id", pa.string()),
            ("session_end_reason", pa.string()),
            ("created_ts", TS),
            ("updated_ts", TS),
        ]
    ),
    "recommendation_requests": pa.schema(
        [
            ("request_id", UUID),
            ("user_id", pa.int64()),
            ("session_id", UUID),
            ("request_timestamp", TS),
            ("surface", pa.string()),
            ("context_product_id", pa.int64()),
            ("context_category_id", pa.int64()),
            ("device_type", pa.string()),
            ("source", pa.string()),
            ("campaign_id", pa.string()),
            ("created_ts", TS),
            ("schema_version", pa.int16()),
        ]
    ),
    "impressions": pa.schema(
        [
            ("impression_id", UUID),
            ("request_id", UUID),
            ("user_id", pa.int64()),
            ("session_id", UUID),
            ("impression_timestamp", TS),
            ("candidate_product_id", pa.int64()),
            ("rank_position", pa.int32()),
            ("candidate_source", pa.string()),
            ("retrieval_score", pa.float64()),
            ("ranking_score", pa.float64()),
            ("surface", pa.string()),
            ("is_clicked", pa.bool_()),
            ("created_ts", TS),
            ("schema_version", pa.int16()),
        ]
    ),
    "behavior_events": pa.schema(
        [
            ("event_id", UUID),
            ("event_timestamp", TS),
            ("created_ts", TS),
            ("ingestion_ts", TS),
            ("user_id", pa.int64()),
            ("session_id", UUID),
            ("request_id", UUID),
            ("impression_id", UUID),
            ("event_type", pa.string()),
            ("product_id", pa.int64()),
            ("category_id", pa.int64()),
            ("brand_id", pa.int64()),
            ("price", MONEY),
            ("price_bucket", pa.int16()),
            ("quantity", pa.int32()),
            ("device_type", pa.string()),
            ("source", pa.string()),
            ("campaign_id", pa.string()),
            ("page_context", pa.string()),
            ("rank_position", pa.int32()),
            ("order_id", UUID),
            ("payload_hash", pa.string()),
            ("event_date", pa.date32()),
            ("schema_version", pa.int16()),
            ("drift_enabled", pa.bool_()),
            ("drift_scenario", pa.string()),
            ("drift_phase", pa.string()),
            ("drift_factor", pa.float64()),
        ]
    ),
    "orders": pa.schema(
        [
            ("order_id", UUID),
            ("user_id", pa.int64()),
            ("session_id", UUID),
            ("order_timestamp", TS),
            ("status", pa.string()),
            ("gross_amount", MONEY),
            ("discount_amount", MONEY),
            ("net_amount", MONEY),
            ("coupon_code", pa.string()),
            ("payment_method", pa.string()),
            ("shipping_city", pa.string()),
            ("paid_ts", TS),
            ("cancelled_ts", TS),
            ("refunded_ts", TS),
            ("created_ts", TS),
            ("updated_ts", TS),
            ("drift_enabled", pa.bool_()),
            ("drift_scenario", pa.string()),
            ("drift_phase", pa.string()),
            ("drift_factor", pa.float64()),
        ]
    ),
    "order_items": pa.schema(
        [
            ("order_item_id", UUID),
            ("order_id", UUID),
            ("product_id", pa.int64()),
            ("quantity", pa.int32()),
            ("unit_price", MONEY),
            ("discount_amount", MONEY),
            ("line_amount", MONEY),
            ("created_ts", TS),
        ]
    ),
}


PARTITION_FIELDS: dict[str, str] = {
    "sessions": "session_start_ts",
    "recommendation_requests": "request_timestamp",
    "impressions": "impression_timestamp",
    "behavior_events": "event_timestamp",
    "orders": "order_timestamp",
    "order_items": "created_ts",
    "product_snapshots": "valid_from",
}
