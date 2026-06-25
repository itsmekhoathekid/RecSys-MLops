from __future__ import annotations

from typing import Any


def latest_product_metadata(products: Any) -> Any:
    from pyspark.sql import Window
    from pyspark.sql import functions as F

    order_column = "valid_from" if "valid_from" in products.columns else "created_ts"
    ranked = products.withColumn(order_column, F.to_timestamp(order_column)).withColumn(
        "_rank",
        F.row_number().over(Window.partitionBy("product_id").orderBy(F.col(order_column).desc_nulls_last())),
    )
    return ranked.filter(F.col("_rank") == 1).drop("_rank")


def build_item_features(
    clean_events: Any,
    products: Any,
    alpha: float = 1.0,
    beta: float = 10.0,
    feature_version: str = "item_features_v1",
) -> Any:
    from pyspark.sql import Window
    from pyspark.sql import functions as F

    events = (
        clean_events.withColumn("event_timestamp", F.to_timestamp("event_timestamp"))
        .withColumn("_event_seconds", F.col("event_timestamp").cast("long"))
    )
    metadata = latest_product_metadata(products).select(
        "product_id",
        F.col("category_id").alias("meta_category_id"),
        F.col("brand_id").alias("meta_brand_id"),
        F.col("price_bucket").alias("meta_price_bucket"),
        F.coalesce(F.col("is_active"), F.lit(True)).alias("meta_is_active"),
    )
    joined = events.join(metadata, on="product_id", how="left")
    by_product = Window.partitionBy("product_id").orderBy("_event_seconds")
    w1h = by_product.rangeBetween(-60 * 60, 0)
    w24h = by_product.rangeBetween(-24 * 60 * 60, 0)
    w7d = by_product.rangeBetween(-7 * 24 * 60 * 60, 0)
    views_7d = F.sum(F.when(F.col("event_type") == "view", 1).otherwise(0)).over(w7d)
    purchases_7d = F.sum(F.when(F.col("event_type") == "purchase", 1).otherwise(0)).over(w7d)
    views_24h = F.sum(F.when(F.col("event_type") == "view", 1).otherwise(0)).over(w24h)
    carts_24h = F.sum(F.when(F.col("event_type") == "cart", 1).otherwise(0)).over(w24h)
    purchases_24h = F.sum(F.when(F.col("event_type") == "purchase", 1).otherwise(0)).over(w24h)
    return joined.select(
        F.col("product_id").cast("int"),
        F.col("event_timestamp").alias("feature_timestamp"),
        F.col("event_timestamp"),
        F.coalesce(F.col("meta_category_id"), F.col("category_id")).cast("int").alias("category_id"),
        F.coalesce(F.col("meta_brand_id"), F.col("brand_id")).cast("int").alias("brand_id"),
        F.coalesce(F.col("meta_price_bucket"), F.col("price_bucket")).cast("int").alias("price_bucket"),
        F.coalesce(F.col("meta_is_active"), F.lit(True)).alias("is_active"),
        F.sum(F.when(F.col("event_type") == "view", 1).otherwise(0)).over(w1h).alias("views_1h"),
        views_24h.alias("views_24h"),
        F.sum(F.when(F.col("event_type") == "cart", 1).otherwise(0)).over(w1h).alias("carts_1h"),
        carts_24h.alias("carts_24h"),
        purchases_24h.alias("purchases_24h"),
        purchases_7d.alias("purchases_7d"),
        ((purchases_7d + F.lit(float(alpha))) / (views_7d + F.lit(float(beta)))).alias("conversion_rate_7d"),
        (views_24h + (carts_24h * F.lit(3)) + (purchases_24h * F.lit(10))).cast("double").alias("popularity_score"),
        F.col("event_timestamp").alias("aggregation_window_end_ts"),
        F.col("event_timestamp").alias("watermark_ts"),
        F.current_timestamp().alias("created_timestamp"),
        F.lit(feature_version).alias("feature_version"),
    )
