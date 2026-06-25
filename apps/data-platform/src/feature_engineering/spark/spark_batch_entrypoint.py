from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import yaml

from feature_engineering.spark.build_bst_training_table import build_bst_training_table
from feature_engineering.spark.build_item_features import build_item_features
from feature_engineering.spark.build_ranking_labels import build_ranking_labels
from feature_engineering.spark.build_silver_tables import build_silver_tables
from feature_engineering.spark.build_user_aggregate_features import build_user_aggregate_features
from feature_engineering.spark.build_user_sequence_features import build_user_sequence_features
from feature_engineering.spark.session import row_count, spark_session, write_parquet


def load_config(config_path: str | Path) -> dict:
    with Path(config_path).open("r", encoding="utf-8") as file:
        return yaml.safe_load(file)


def run_pyspark_batch(config_path: str | Path = "configs/local/spark_batch.yaml") -> dict[str, int]:
    spark = spark_session("recsys-pyspark-batch-features")
    config = load_config(config_path)
    configured_run_path = Path(config["input"]["run_path"])
    output = config["output"]
    features = config["features"]
    if os.getenv("DATAFLOW_OUTPUT_MODE") == "s3":
        run_path = f"s3a://{output['lake_bucket']}/raw/{configured_run_path.name}"
        silver_base = output["lake_silver_uri"]
        offline_base = output["feature_store_offline_uri"]
        ml_base = output["ml_artifact_uri"]
    else:
        run_path = str(configured_run_path)
        silver_base = output["silver_path"]
        offline_base = output["offline_feature_path"]
        ml_base = output["ml_artifact_path"]

    silver = build_silver_tables(spark, run_path, silver_base)
    user_sequence = build_user_sequence_features(
        silver["clean_behavior_events"],
        max_history_length=features["max_history_length"],
    )
    user_aggregate = build_user_aggregate_features(silver["clean_behavior_events"])
    item_features = build_item_features(
        silver["clean_behavior_events"],
        silver["product_scd"],
        alpha=features["conversion_smoothing_alpha"],
        beta=features["conversion_smoothing_beta"],
    )
    labels = build_ranking_labels(
        silver["clean_impressions"],
        silver["clean_behavior_events"],
        label_window_hours=features["label_window_hours"],
    )
    training = build_bst_training_table(
        labels,
        user_sequence,
        user_aggregate,
        item_features,
        max_history_length=features["max_history_length"],
    )

    outputs = {
        f"{offline_base.rstrip('/')}/user_sequence_features": user_sequence,
        f"{offline_base.rstrip('/')}/user_aggregate_features": user_aggregate,
        f"{offline_base.rstrip('/')}/item_features": item_features,
        f"{ml_base.rstrip('/')}/ml_ranking_labels": labels,
        f"{ml_base.rstrip('/')}/ml_bst_training": training,
    }
    for path, frame in outputs.items():
        write_parquet(frame, path)

    summary = {
        "clean_behavior_events": row_count(silver["clean_behavior_events"]),
        "user_sequence_features": row_count(user_sequence),
        "user_aggregate_features": row_count(user_aggregate),
        "item_features": row_count(item_features),
        "ml_ranking_labels": row_count(labels),
        "ml_bst_training": row_count(training),
    }
    spark.stop()
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Run PySpark batch feature flow")
    parser.add_argument("--config", default="configs/local/spark_batch.yaml")
    args = parser.parse_args()
    print(json.dumps(run_pyspark_batch(args.config), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
