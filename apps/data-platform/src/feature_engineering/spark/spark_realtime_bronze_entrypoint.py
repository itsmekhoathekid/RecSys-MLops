from __future__ import annotations

import argparse
import json
import os

from feature_engineering.spark.build_item_features import build_item_features
from feature_engineering.spark.build_user_aggregate_features import (
    build_user_aggregate_features,
)
from feature_engineering.spark.build_user_sequence_features import (
    build_user_sequence_features,
)
from feature_engineering.spark.session import row_count, spark_session, write_parquet


def main() -> int:
    parser = argparse.ArgumentParser(description="Build realtime offline features from Kafka bronze CDC data.")
    parser.add_argument(
        "--bronze-root",
        default=f"s3://{os.getenv('LAKE_BUCKET', 'recsys-lake')}/bronze/kafka",
    )
    parser.add_argument(
        "--offline-root",
        default=f"s3://{os.getenv('FEATURE_STORE_BUCKET', 'recsys-feature-store')}/offline",
    )
    parser.add_argument("--topic", default="cdc.behavior_events")
    parser.add_argument("--max-history-length", type=int, default=50)
    args = parser.parse_args()

    from pyspark.sql import functions as F

    spark = spark_session("recsys-pyspark-realtime-bronze-features")
    topic_root = f"{args.bronze_root.rstrip('/')}/{args.topic}"
    raw = spark.read.json(topic_root)
    if "payload" in raw.columns:
        events = raw.select("payload.after.*")
    elif "after" in raw.columns:
        events = raw.select("after.*")
    else:
        events = raw
    clean_events = (
        events
        .filter(F.col("event_id").isNotNull())
        .withColumn("event_timestamp", F.to_timestamp("event_timestamp"))
        .withColumn(
            "event_type_id",
            F.when(F.col("event_type") == "view", F.lit(1))
            .when(F.col("event_type") == "cart", F.lit(2))
            .when(F.col("event_type") == "purchase", F.lit(3))
            .otherwise(F.lit(0)),
        )
    )
    if clean_events.limit(1).count() == 0:
        raise SystemExit(f"No bronze CDC behavior events found under {args.bronze_root}")

    products = clean_events.select("product_id", "category_id", "brand_id", "price_bucket").dropDuplicates(["product_id"])
    products = products.withColumn("is_active", F.lit(True))

    user_sequence = build_user_sequence_features(
        clean_events,
        max_history_length=args.max_history_length,
    )
    user_aggregate = build_user_aggregate_features(clean_events)
    item_features = build_item_features(clean_events, products)

    offline_root = args.offline_root.rstrip("/")
    write_parquet(user_sequence, f"{offline_root}/user_sequence_features")
    write_parquet(user_aggregate, f"{offline_root}/user_aggregate_features")
    write_parquet(item_features, f"{offline_root}/item_features")

    print(
        json.dumps(
            {
                "bronze_behavior_events": row_count(clean_events),
                "user_sequence_features": row_count(user_sequence),
                "user_aggregate_features": row_count(user_aggregate),
                "item_features": row_count(item_features),
            },
            indent=2,
            sort_keys=True,
        )
    )
    spark.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
