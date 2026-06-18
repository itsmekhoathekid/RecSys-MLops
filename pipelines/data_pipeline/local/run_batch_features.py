from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import yaml

from pipelines.data_pipeline.feature_engineering.spark.build_bst_training_table import (
    build_bst_training_table,
)
from pipelines.data_pipeline.feature_engineering.spark.build_item_features import (
    build_item_features,
)
from pipelines.data_pipeline.feature_engineering.spark.build_ranking_labels import (
    build_ranking_labels,
)
from pipelines.data_pipeline.feature_engineering.spark.build_silver_tables import (
    build_silver_tables,
)
from pipelines.data_pipeline.feature_engineering.spark.build_user_aggregate_features import (
    build_user_aggregate_features,
)
from pipelines.data_pipeline.feature_engineering.spark.build_user_sequence_features import (
    build_user_sequence_features,
)
from pipelines.data_pipeline.feature_store.offline_writer import write_feature_table


def load_config(config_path: str | Path) -> dict:
    with Path(config_path).open("r", encoding="utf-8") as file:
        return yaml.safe_load(file)


def join_output_path(base: str | Path, child: str) -> str | Path:
    if isinstance(base, Path):
        return base / child
    return base.rstrip("/") + "/" + child


def run_batch_features(config_path: str | Path = "config/spark_batch.yaml") -> dict[str, int]:
    config = load_config(config_path)
    configured_run_path = Path(config["input"]["run_path"])
    output = config["output"]
    features = config["features"]
    if os.getenv("DATAFLOW_OUTPUT_MODE") == "s3":
        run_id = configured_run_path.name
        run_path: str | Path = f"s3://{output['lake_bucket']}/raw/{run_id}"
    else:
        run_path = configured_run_path
    silver = build_silver_tables(run_path, output["silver_path"])
    user_sequence = build_user_sequence_features(
        silver["clean_behavior_events"],
        max_history_length=features["max_history_length"],
    )
    user_aggregate = build_user_aggregate_features(silver["clean_behavior_events"])
    item_features = build_item_features(
        silver["clean_behavior_events"],
        silver["products"],
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

    if os.getenv("DATAFLOW_OUTPUT_MODE") == "s3":
        offline_base = output["feature_store_offline_uri"].replace("s3a://", "s3://")
        ml_base = output["ml_artifact_uri"].replace("s3a://", "s3://")
        silver_base = output["lake_silver_uri"].replace("s3a://", "s3://")
    else:
        offline_base = Path(output["offline_feature_path"])
        ml_base = Path(output["ml_artifact_path"])
        silver_base = Path(output["silver_path"])
    write_feature_table(silver["clean_behavior_events"], join_output_path(silver_base, "clean_behavior_events"))
    write_feature_table(silver["clean_impressions"], join_output_path(silver_base, "clean_impressions"))
    write_feature_table(silver["order_facts"], join_output_path(silver_base, "order_facts"))
    write_feature_table(silver["product_scd"], join_output_path(silver_base, "product_scd"))
    write_feature_table(user_sequence, join_output_path(offline_base, "user_sequence_features"))
    write_feature_table(user_aggregate, join_output_path(offline_base, "user_aggregate_features"))
    write_feature_table(item_features, join_output_path(offline_base, "item_features"))
    write_feature_table(labels, join_output_path(ml_base, "ml_ranking_labels"))
    write_feature_table(training, join_output_path(ml_base, "ml_bst_training"))

    return {
        "clean_behavior_events": len(silver["clean_behavior_events"]),
        "user_sequence_features": len(user_sequence),
        "user_aggregate_features": len(user_aggregate),
        "item_features": len(item_features),
        "ml_ranking_labels": len(labels),
        "ml_bst_training": len(training),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run local batch feature flow")
    parser.add_argument("--config", default="config/spark_batch.yaml")
    args = parser.parse_args()
    print(json.dumps(run_batch_features(args.config), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
