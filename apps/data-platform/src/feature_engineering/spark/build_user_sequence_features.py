from __future__ import annotations

from typing import Any


SEQUENCE_COLUMNS = [
    "hist_item_ids",
    "hist_event_type_ids",
    "hist_category_ids",
    "hist_brand_ids",
    "hist_price_bucket_ids",
    "hist_event_timestamps",
    "hist_request_ids",
    "hist_impression_ids",
]


def build_user_sequence_features(
    clean_events: Any,
    max_history_length: int = 50,
    feature_version: str = "bst_sequence_v2",
) -> Any:
    from pyspark.sql import Window
    from pyspark.sql import functions as F

    events = clean_events.withColumn("event_timestamp", F.to_timestamp("event_timestamp"))
    window = (
        Window.partitionBy("user_id")
        .orderBy("event_timestamp", "event_id")
        .rowsBetween(-max_history_length + 1, 0)
    )
    history = F.collect_list(
        F.struct(
            F.col("product_id").cast("int").alias("product_id"),
            F.col("event_type_id").cast("int").alias("event_type_id"),
            F.col("category_id").cast("int").alias("category_id"),
            F.col("brand_id").cast("int").alias("brand_id"),
            F.col("price_bucket").cast("int").alias("price_bucket"),
            F.date_format("event_timestamp", "yyyy-MM-dd'T'HH:mm:ssXXX").alias("event_timestamp"),
            F.coalesce(F.col("request_id").cast("string"), F.lit("")).alias("request_id"),
            F.coalesce(F.col("impression_id").cast("string"), F.lit("")).alias("impression_id"),
        )
    ).over(window)
    return (
        events.withColumn("_history", history)
        .select(
            F.col("user_id").cast("int"),
            F.col("event_timestamp").alias("feature_timestamp"),
            F.col("event_timestamp"),
            F.current_timestamp().alias("created_timestamp"),
            F.expr("transform(_history, x -> x.product_id)").alias("hist_item_ids"),
            F.expr("transform(_history, x -> x.event_type_id)").alias("hist_event_type_ids"),
            F.expr("transform(_history, x -> x.category_id)").alias("hist_category_ids"),
            F.expr("transform(_history, x -> x.brand_id)").alias("hist_brand_ids"),
            F.expr("transform(_history, x -> x.price_bucket)").alias("hist_price_bucket_ids"),
            F.expr("transform(_history, x -> x.event_timestamp)").alias("hist_event_timestamps"),
            F.expr("transform(_history, x -> x.request_id)").alias("hist_request_ids"),
            F.expr("transform(_history, x -> x.impression_id)").alias("hist_impression_ids"),
            F.size("_history").alias("hist_length"),
            F.lit(max_history_length).alias("max_history_length"),
            F.lit(feature_version).alias("feature_version"),
        )
    )
