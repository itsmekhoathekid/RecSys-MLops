from __future__ import annotations

import os
from datetime import timedelta

from feast import Entity, FeatureView, Field, FileSource
from feast.types import Bool, Float64, Int64, String


FEAST_OFFLINE_ROOT = os.getenv(
    "FEAST_OFFLINE_ROOT",
    "s3://recsys-offline-feature-store/feast/offline",
).rstrip("/")


user = Entity(name="user", join_keys=["user_id"])
product = Entity(name="product", join_keys=["product_id"])


user_aggregate_source = FileSource(
    name="user_aggregate_features_source",
    path=f"{FEAST_OFFLINE_ROOT}/user_aggregate_features",
    timestamp_field="feature_timestamp",
)

item_features_source = FileSource(
    name="item_features_source",
    path=f"{FEAST_OFFLINE_ROOT}/item_features",
    timestamp_field="feature_timestamp",
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
)
