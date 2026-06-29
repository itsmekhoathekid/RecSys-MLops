from __future__ import annotations

from typing import Any


def _latest_asof(labels: Any, features: Any, label_key: str, feature_key: str, prefix: str) -> Any:
    from pyspark.sql import Window
    from pyspark.sql import functions as F

    label_cols = [F.col(f"l.{column}").alias(column) for column in labels.columns]
    feature_cols = [
        F.col(f"f.{column}").alias(f"{prefix}_{column}")
        for column in features.columns
        if column != feature_key
    ]
    joined = labels.alias("l").join(
        features.alias("f"),
        (F.col(f"l.{label_key}") == F.col(f"f.{feature_key}"))
        & (F.col("f.feature_timestamp") <= F.col("l.prediction_timestamp")),
        "left",
    )
    window = Window.partitionBy("l.impression_id", "l.candidate_product_id").orderBy(
        F.col("f.feature_timestamp").desc_nulls_last()
    )
    return joined.withColumn("_asof_rank", F.row_number().over(window)).filter(F.col("_asof_rank") == 1).select(
        *label_cols,
        *feature_cols,
    )


def build_bst_training_table(
    labels: Any,
    user_sequence_features: Any,
    user_aggregate_features: Any,
    item_features: Any,
    max_history_length: int = 50,
) -> Any:
    from pyspark.sql import functions as F

    with_sequence = _latest_asof(labels, user_sequence_features, "user_id", "user_id", "seq")
    with_aggregate = _latest_asof(with_sequence, user_aggregate_features, "user_id", "user_id", "agg")
    with_item = _latest_asof(with_aggregate, item_features, "candidate_product_id", "product_id", "item")
    empty_int_array = F.array().cast("array<int>")
    empty_string_array = F.array().cast("array<string>")
    return with_item.select(
        "impression_id",
        "request_id",
        F.col("user_id").cast("int"),
        F.coalesce(F.col("seq_hist_item_ids"), empty_int_array).alias("hist_item_id"),
        F.coalesce(F.col("seq_hist_event_type_ids"), empty_int_array).alias("hist_event_type"),
        F.coalesce(F.col("seq_hist_category_ids"), empty_int_array).alias("hist_category"),
        F.coalesce(F.col("seq_hist_brand_ids"), empty_int_array).alias("hist_brand"),
        F.coalesce(F.col("seq_hist_price_bucket_ids"), empty_int_array).alias("hist_price_bucket"),
        F.transform(F.coalesce(F.col("seq_hist_event_timestamps"), empty_string_array), lambda _: F.lit(1)).alias("hist_time"),
        F.col("candidate_product_id").cast("int").alias("target_item_id"),
        F.coalesce(F.col("item_category_id"), F.lit(0)).cast("int").alias("target_category"),
        F.coalesce(F.col("item_brand_id"), F.lit(0)).cast("int").alias("target_brand"),
        F.coalesce(F.col("item_price_bucket"), F.lit(0)).cast("int").alias("target_price_bucket"),
        F.col("prediction_timestamp").cast("long").alias("event_time"),
        "prediction_timestamp",
        F.col("label").cast("int"),
        F.coalesce(F.col("agg_views_30m"), F.lit(0)).cast("int").alias("views_30m"),
        F.coalesce(F.col("agg_carts_30m"), F.lit(0)).cast("int").alias("carts_30m"),
        F.coalesce(F.col("agg_purchases_24h"), F.lit(0)).cast("int").alias("purchases_24h"),
        F.lit(max_history_length).alias("max_history_length"),
    )
