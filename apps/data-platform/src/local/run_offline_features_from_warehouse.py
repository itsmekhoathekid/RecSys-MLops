from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from feature_engineering.spark.build_bst_training_table import build_bst_training_table
from feature_engineering.spark.build_item_features import build_item_features
from feature_engineering.spark.build_ranking_labels import build_ranking_labels
from feature_engineering.spark.build_user_aggregate_features import build_user_aggregate_features
from feature_engineering.spark.build_user_sequence_features import build_user_sequence_features
from feature_store.offline_writer import write_feature_table
from warehouse.connection import connect
from warehouse.reader import read_production_tables


def join_output_path(base: str | Path, child: str) -> str | Path:
    if isinstance(base, Path):
        return base / child
    return base.rstrip("/") + "/" + child


def run_offline_features_from_warehouse(
    offline_root: str | Path,
    ml_root: str | Path,
    max_history_length: int = 50,
    label_window_hours: int = 24,
) -> dict[str, int]:
    with connect() as connection:
        tables = read_production_tables(connection)

    clean_events = tables["fact_behavior_events"]
    impressions = tables["fact_impressions"]
    products = tables["dim_products_scd"]
    if clean_events.empty:
        raise SystemExit("production.fact_behavior_events is empty; run dbt before offline feature export")
    if impressions.empty:
        raise SystemExit("production.fact_impressions is empty; run dbt before label export")
    if products.empty:
        raise SystemExit("production.dim_products_scd is empty; run dbt before item feature export")

    user_sequence = build_user_sequence_features(clean_events, max_history_length=max_history_length)
    user_aggregate = build_user_aggregate_features(clean_events)
    item_features = build_item_features(clean_events, products)
    labels = build_ranking_labels(impressions, clean_events, label_window_hours=label_window_hours)
    training = build_bst_training_table(
        labels,
        user_sequence,
        user_aggregate,
        item_features,
        max_history_length=max_history_length,
    )

    write_feature_table(user_sequence, join_output_path(offline_root, "user_sequence_features"))
    write_feature_table(user_aggregate, join_output_path(offline_root, "user_aggregate_features"))
    write_feature_table(item_features, join_output_path(offline_root, "item_features"))
    write_feature_table(labels, join_output_path(ml_root, "ml_ranking_labels"))
    write_feature_table(training, join_output_path(ml_root, "ml_bst_training"))

    return {
        "user_sequence_features": len(user_sequence),
        "user_aggregate_features": len(user_aggregate),
        "item_features": len(item_features),
        "ml_ranking_labels": len(labels),
        "ml_bst_training": len(training),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Export Feast offline features from dbt production warehouse tables.")
    parser.add_argument(
        "--offline-root",
        default=os.getenv("FEAST_OFFLINE_ROOT", "s3://recsys-feature-store/offline"),
    )
    parser.add_argument(
        "--ml-root",
        default=os.getenv("ML_ARTIFACT_ROOT", "s3://recsys-lake/silver/ml"),
    )
    parser.add_argument("--max-history-length", type=int, default=50)
    parser.add_argument("--label-window-hours", type=int, default=24)
    args = parser.parse_args()
    print(
        json.dumps(
            run_offline_features_from_warehouse(
                args.offline_root,
                args.ml_root,
                max_history_length=args.max_history_length,
                label_window_hours=args.label_window_hours,
            ),
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

