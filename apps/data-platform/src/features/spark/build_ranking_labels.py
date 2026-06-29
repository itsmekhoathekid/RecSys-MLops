from __future__ import annotations

from typing import Any


def build_ranking_labels(
    impressions: Any,
    clean_events: Any,
    label_window_hours: int = 24,
    label_version: str = "ranking_label_v1",
) -> Any:
    from pyspark.sql import functions as F

    imps = impressions.withColumn("prediction_timestamp", F.to_timestamp("impression_timestamp")).withColumn(
        "label_window_end",
        F.expr(f"prediction_timestamp + INTERVAL {int(label_window_hours)} HOURS"),
    )
    positives = (
        clean_events.withColumn("positive_event_timestamp", F.to_timestamp("event_timestamp"))
        .filter(F.col("event_type").isin("cart", "purchase"))
        .select(
            F.col("user_id").alias("event_user_id"),
            F.col("product_id").alias("event_product_id"),
            F.col("event_type").alias("positive_event_type"),
            "positive_event_timestamp",
        )
    )
    joined = imps.join(
        positives,
        (imps.user_id == positives.event_user_id)
        & (imps.candidate_product_id == positives.event_product_id)
        & (positives.positive_event_timestamp > imps.prediction_timestamp)
        & (positives.positive_event_timestamp <= imps.label_window_end),
        "left",
    )
    grouped = joined.groupBy(
        "impression_id",
        "request_id",
        "user_id",
        "candidate_product_id",
        "prediction_timestamp",
        "label_window_end",
        "candidate_source",
        "rank_position",
    ).agg(
        F.min("positive_event_timestamp").alias("positive_event_timestamp"),
        F.first("positive_event_type", ignorenulls=True).alias("positive_event_type"),
    )
    return grouped.select(
        F.col("impression_id").cast("string"),
        F.col("request_id").cast("string"),
        F.col("user_id").cast("int"),
        F.col("candidate_product_id").cast("int"),
        "prediction_timestamp",
        "label_window_end",
        F.when(F.col("positive_event_timestamp").isNotNull(), 1).otherwise(0).alias("label"),
        "positive_event_type",
        "positive_event_timestamp",
        F.lit("impression").alias("sampling_strategy"),
        F.lit(1.0).alias("sampling_probability"),
        F.coalesce(F.col("candidate_source"), F.lit("unknown")).alias("candidate_source"),
        F.col("rank_position").cast("int"),
        F.current_timestamp().alias("created_timestamp"),
        F.lit(label_version).alias("label_version"),
    )
