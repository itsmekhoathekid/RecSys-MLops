from __future__ import annotations

from typing import Any


CATEGORY_CARDINALITY_RSD = 0.05


def build_user_aggregate_features(
    clean_events: Any,
    feature_version: str = "user_aggregate_v1",
) -> Any:
    from pyspark.sql import Window
    from pyspark.sql import functions as F

    events = (
        clean_events.withColumn("event_timestamp", F.to_timestamp("event_timestamp"))
        .withColumn("_event_seconds", F.col("event_timestamp").cast("long"))
        .withColumn("price", F.col("price").cast("double"))
    )
    by_user = Window.partitionBy("user_id").orderBy("_event_seconds")
    w30m = by_user.rangeBetween(-30 * 60, 0)
    w24h = by_user.rangeBetween(-24 * 60 * 60, 0)
    w7d = by_user.rangeBetween(-7 * 24 * 60 * 60, 0)
    carts_7d = F.sum(F.when(F.col("event_type") == "cart", 1).otherwise(0)).over(w7d)
    purchases_7d = F.sum(F.when(F.col("event_type") == "purchase", 1).otherwise(0)).over(w7d)
    viewed_price_sum = F.sum(F.when(F.col("event_type") == "view", F.col("price")).otherwise(0.0)).over(w7d)
    view_count_7d = F.sum(F.when(F.col("event_type") == "view", 1).otherwise(0)).over(w7d)
    return events.select(
        F.col("user_id").cast("int"),
        F.col("event_timestamp").alias("feature_timestamp"),
        F.col("event_timestamp"),
        F.sum(F.when(F.col("event_type") == "view", 1).otherwise(0)).over(w30m).alias("views_30m"),
        F.sum(F.when(F.col("event_type") == "cart", 1).otherwise(0)).over(w30m).alias("carts_30m"),
        F.sum(F.when(F.col("event_type") == "purchase", 1).otherwise(0)).over(w24h).alias("purchases_24h"),
        F.approx_count_distinct("category_id", CATEGORY_CARDINALITY_RSD)
        .over(w7d)
        .alias("distinct_categories_7d"),
        F.when(view_count_7d > 0, viewed_price_sum / view_count_7d).otherwise(F.lit(0.0)).alias("avg_viewed_price_7d"),
        F.when(carts_7d > 0, purchases_7d / carts_7d).otherwise(F.lit(0.0)).alias("cart_to_purchase_ratio_7d"),
        F.lit(0).alias("last_event_age_seconds"),
        F.col("event_timestamp").alias("aggregation_window_end_ts"),
        F.col("event_timestamp").alias("watermark_ts"),
        F.current_timestamp().alias("created_timestamp"),
        F.lit(feature_version).alias("feature_version"),
    )
