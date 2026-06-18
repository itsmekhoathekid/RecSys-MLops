from __future__ import annotations

import argparse
import json
import os

import pandas as pd

from pipelines.data_pipeline.feature_engineering.spark.build_item_features import build_item_features
from pipelines.data_pipeline.feature_engineering.spark.build_user_aggregate_features import (
    build_user_aggregate_features,
)
from pipelines.data_pipeline.feature_engineering.spark.build_user_sequence_features import (
    build_user_sequence_features,
)
from pipelines.data_pipeline.feature_store.offline_writer import write_feature_table
from pipelines.data_pipeline.ingest.bronze_cdc_reader import (
    normalize_behavior_events_from_cdc,
    read_bronze_cdc_table,
)


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

    raw_events = read_bronze_cdc_table(args.bronze_root, args.topic)
    clean_events = normalize_behavior_events_from_cdc(raw_events)
    if clean_events.empty:
        raise SystemExit(f"No bronze CDC behavior events found under {args.bronze_root}")

    products = clean_events[
        ["product_id", "category_id", "brand_id", "price_bucket"]
    ].drop_duplicates("product_id")
    products["is_active"] = True

    user_sequence = build_user_sequence_features(
        clean_events,
        max_history_length=args.max_history_length,
    )
    user_aggregate = build_user_aggregate_features(clean_events)
    item_features = build_item_features(clean_events, products)

    offline_root = args.offline_root.rstrip("/")
    write_feature_table(user_sequence, f"{offline_root}/user_sequence_features")
    write_feature_table(user_aggregate, f"{offline_root}/user_aggregate_features")
    write_feature_table(item_features, f"{offline_root}/item_features")

    print(
        json.dumps(
            {
                "bronze_behavior_events": len(clean_events),
                "user_sequence_features": len(user_sequence),
                "user_aggregate_features": len(user_aggregate),
                "item_features": len(item_features),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
