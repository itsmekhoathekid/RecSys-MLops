from __future__ import annotations

import os
from datetime import timedelta

from feast import Entity, FeatureService, FeatureView, Field, FileSource, ValueType
from feast.types import Array, Bool, Float64, Int64, String


FEAST_OFFLINE_ROOT = os.getenv(
    "FEAST_OFFLINE_ROOT",
    "s3://recsys-offline-feature-store/feast/offline",
).rstrip("/")
FEAST_S3_ENDPOINT = (
    os.getenv("FEAST_S3_ENDPOINT_OVERRIDE")
    or os.getenv("MINIO_ENDPOINT")
    or os.getenv("DATA_PLATFORM_MINIO_ENDPOINT")
)


user = Entity(name="user", join_keys=["user_id"], value_type=ValueType.INT64)
product = Entity(name="product", join_keys=["product_id"], value_type=ValueType.INT64)


user_sequence_source = FileSource(
    name="user_sequence_features_source",
    path=f"{FEAST_OFFLINE_ROOT}/user_sequence_features",
    timestamp_field="feature_timestamp",
    s3_endpoint_override=FEAST_S3_ENDPOINT,
)

user_aggregate_source = FileSource(
    name="user_aggregate_features_source",
    path=f"{FEAST_OFFLINE_ROOT}/user_aggregate_features",
    timestamp_field="feature_timestamp",
    s3_endpoint_override=FEAST_S3_ENDPOINT,
)

item_features_source = FileSource(
    name="item_features_source",
    path=f"{FEAST_OFFLINE_ROOT}/item_features",
    timestamp_field="feature_timestamp",
    s3_endpoint_override=FEAST_S3_ENDPOINT,
)


user_sequence_features = FeatureView(
    name="user_sequence_features",
    entities=[user],
    ttl=timedelta(days=1),
    schema=[
        Field(name="hist_item_ids", dtype=Array(Int64)),
        Field(name="hist_event_type_ids", dtype=Array(Int64)),
        Field(name="hist_category_ids", dtype=Array(Int64)),
        Field(name="hist_brand_ids", dtype=Array(Int64)),
        Field(name="hist_price_bucket_ids", dtype=Array(Int64)),
        Field(name="hist_event_timestamps", dtype=Array(String)),
        Field(name="hist_request_ids", dtype=Array(String)),
        Field(name="hist_impression_ids", dtype=Array(String)),
        Field(name="hist_length", dtype=Int64),
        Field(name="max_history_length", dtype=Int64),
        Field(name="feature_version", dtype=String),
    ],
    source=user_sequence_source,
    online=True,
    tags={"offline_store": "apache_iceberg", "online_store": "redis"},
)


user_aggregate_features = FeatureView(
    name="user_aggregate_features",
    entities=[user],
    ttl=timedelta(days=1),
    schema=[
        Field(name="views_30m", dtype=Int64),
        Field(name="carts_30m", dtype=Int64),
        Field(name="purchases_24h", dtype=Int64),
        Field(name="distinct_categories_7d", dtype=Int64),
        Field(name="avg_viewed_price_7d", dtype=Float64),
        Field(name="cart_to_purchase_ratio_7d", dtype=Float64),
        Field(name="last_event_age_seconds", dtype=Int64),
        Field(name="feature_version", dtype=String),
    ],
    source=user_aggregate_source,
    online=True,
    tags={"offline_store": "apache_iceberg", "online_store": "redis"},
)


item_features = FeatureView(
    name="item_features",
    entities=[product],
    ttl=timedelta(days=7),
    schema=[
        Field(name="category_id", dtype=Int64),
        Field(name="brand_id", dtype=Int64),
        Field(name="price_bucket", dtype=Int64),
        Field(name="is_active", dtype=Bool),
        Field(name="views_1h", dtype=Int64),
        Field(name="views_24h", dtype=Int64),
        Field(name="carts_1h", dtype=Int64),
        Field(name="carts_24h", dtype=Int64),
        Field(name="purchases_24h", dtype=Int64),
        Field(name="purchases_7d", dtype=Int64),
        Field(name="conversion_rate_7d", dtype=Float64),
        Field(name="popularity_score", dtype=Float64),
        Field(name="feature_version", dtype=String),
    ],
    source=item_features_source,
    online=True,
    tags={"offline_store": "apache_iceberg", "online_store": "redis"},
)


bst_ranking_v1 = FeatureService(
    name="bst_ranking_v1",
    features=[
        user_sequence_features,
        user_aggregate_features,
        item_features,
    ],
    tags={"offline_store": "apache_iceberg", "online_store": "redis"},
)
