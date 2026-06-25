from __future__ import annotations

import json
from pathlib import Path

import yaml

from feature_engineering.spark.session import spark_session
from feature_engineering.spark.spark_batch_entrypoint import run_pyspark_batch


def main() -> int:
    spark = spark_session("recsys-pyspark-mini-input")
    base = "/tmp/recsys-spark-mini-input"
    rows = {
        "users": [{"user_id": 1, "city": "hcmc", "created_ts": "2026-01-01T00:00:00Z"}],
        "user_preferences": [
            {"user_id": 1, "category_id": 2, "brand_id": 3, "preference_weight": 0.8, "updated_ts": "2026-01-01T00:00:00Z"}
        ],
        "products": [
            {
                "product_id": 10,
                "category_id": 2,
                "brand_id": 3,
                "price_bucket": 4,
                "is_active": True,
                "created_ts": "2026-01-01T00:00:00Z",
            }
        ],
        "product_snapshots": [
            {
                "product_id": 10,
                "category_id": 2,
                "brand_id": 3,
                "price_bucket": 4,
                "is_active": True,
                "valid_from": "2026-01-01T00:00:00Z",
                "valid_to": "2027-01-01T00:00:00Z",
            }
        ],
        "sessions": [{"session_id": "s1", "user_id": 1, "session_start_ts": "2026-01-01T00:00:00Z"}],
        "recommendation_requests": [
            {"request_id": "r1", "user_id": 1, "request_timestamp": "2026-01-01T00:01:00Z", "request_context": "{}"}
        ],
        "impressions": [
            {
                "impression_id": "i1",
                "request_id": "r1",
                "user_id": 1,
                "candidate_product_id": 10,
                "impression_timestamp": "2026-01-01T00:02:00Z",
                "rank_position": 1,
                "candidate_source": "popular",
            }
        ],
        "behavior_events": [
            {
                "event_id": "e1",
                "payload_hash": "h1",
                "ingestion_ts": "2026-01-01T00:01:01Z",
                "event_timestamp": "2026-01-01T00:01:00Z",
                "user_id": 1,
                "product_id": 10,
                "event_type": "view",
                "category_id": 2,
                "brand_id": 3,
                "price_bucket": 4,
                "price": 9.0,
                "request_id": "r0",
                "impression_id": "i0",
            },
            {
                "event_id": "e2",
                "payload_hash": "h2",
                "ingestion_ts": "2026-01-01T00:03:01Z",
                "event_timestamp": "2026-01-01T00:03:00Z",
                "user_id": 1,
                "product_id": 10,
                "event_type": "cart",
                "category_id": 2,
                "brand_id": 3,
                "price_bucket": 4,
                "price": 9.0,
                "request_id": "r1",
                "impression_id": "i1",
            },
        ],
        "orders": [
            {
                "order_id": "o1",
                "user_id": 1,
                "status": "completed",
                "order_timestamp": "2026-01-01T00:04:00Z",
                "created_ts": "2026-01-01T00:04:00Z",
            }
        ],
        "order_items": [
            {"order_item_id": "oi1", "order_id": "o1", "product_id": 10, "quantity": 1, "price": 9.0, "created_ts": "2026-01-01T00:04:00Z"}
        ],
    }
    for table, data in rows.items():
        spark.createDataFrame(data).write.mode("overwrite").parquet(f"{base}/{table}")
    spark.stop()

    config = {
        "input": {"run_path": base},
        "output": {
            "silver_path": "/tmp/recsys-spark-mini-output/silver",
            "offline_feature_path": "/tmp/recsys-spark-mini-output/offline",
            "ml_artifact_path": "/tmp/recsys-spark-mini-output/ml",
            "lake_bucket": "recsys-lake",
            "feature_store_bucket": "recsys-feature-store",
            "lake_silver_uri": "s3a://recsys-lake/silver",
            "feature_store_offline_uri": "s3a://recsys-feature-store/offline",
            "ml_artifact_uri": "s3a://recsys-lake/silver/ml",
        },
        "features": {
            "max_history_length": 50,
            "label_window_hours": 24,
            "conversion_smoothing_alpha": 1.0,
            "conversion_smoothing_beta": 10.0,
        },
    }
    config_path = Path("/tmp/recsys-spark-mini.yaml")
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    print(json.dumps(run_pyspark_batch(config_path), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
