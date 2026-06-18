from __future__ import annotations

from datetime import timedelta

try:
    from feast import FeatureView, Field
    from feast.types import Bool, Float32, Int64, String
except ImportError:  # pragma: no cover
    FeatureView = Field = Bool = Float32 = Int64 = String = None

from data_sources import item_features_source, user_aggregate_source, user_sequence_source
from entities import product, user


if FeatureView is not None:
    user_sequence_features = FeatureView(
        name="user_sequence_features",
        entities=[user],
        ttl=timedelta(days=90),
        schema=[
            Field(name="hist_item_ids", dtype=String),
            Field(name="hist_event_type_ids", dtype=String),
            Field(name="hist_category_ids", dtype=String),
            Field(name="hist_brand_ids", dtype=String),
            Field(name="hist_price_bucket_ids", dtype=String),
            Field(name="hist_event_timestamps", dtype=String),
            Field(name="hist_length", dtype=Int64),
            Field(name="max_history_length", dtype=Int64),
            Field(name="feature_version", dtype=String),
        ],
        online=True,
        source=user_sequence_source,
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
            Field(name="avg_viewed_price_7d", dtype=Float32),
            Field(name="cart_to_purchase_ratio_7d", dtype=Float32),
            Field(name="last_event_age_seconds", dtype=Int64),
            Field(name="feature_version", dtype=String),
        ],
        online=True,
        source=user_aggregate_source,
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
            Field(name="conversion_rate_7d", dtype=Float32),
            Field(name="popularity_score", dtype=Float32),
            Field(name="feature_version", dtype=String),
        ],
        online=True,
        source=item_features_source,
    )
else:
    user_sequence_features = user_aggregate_features = item_features = None

