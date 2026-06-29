from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import timedelta
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq

from config import GeneratorConfig, load_config
from sink import read_table


def print_section(title: str) -> None:
    print()
    print(f"## {title}")


def print_markdown_table(headers: list[str], rows: list[list[Any]]) -> None:
    table = [[str(value) for value in headers]]
    table.extend([[str(value) for value in row] for row in rows])
    column_count = max(len(row) for row in table)
    table = [row + [""] * (column_count - len(row)) for row in table]
    widths = [
        max(3, max(len(row[column_index]) for row in table))
        for column_index in range(column_count)
    ]

    def format_row(row: list[str]) -> str:
        return (
            "| "
            + " | ".join(
                row[column_index].ljust(widths[column_index])
                for column_index in range(column_count)
            )
            + " |"
        )

    print(format_row(table[0]))
    print("| " + " | ".join("-" * width for width in widths) + " |")
    for row in table[1:]:
        print(format_row(row))


def build_label_rows(config: GeneratorConfig, run_path: Path) -> list[dict[str, Any]]:
    users = read_table(run_path, "users").to_pylist()
    orders = read_table(run_path, "orders").to_pylist()
    start_date = config.drift.drift_start_date or config.history_start_date

    post_drift_orders_by_user: dict[int, int] = defaultdict(int)
    for order in orders:
        if order["order_timestamp"].date() >= start_date:
            post_drift_orders_by_user[order["user_id"]] += 1

    labels = []
    for user in users:
        user_id = user["user_id"]
        labels.append(
            {
                "user_id": user_id,
                "label": 1 if post_drift_orders_by_user[user_id] > 0 else 0,
                "post_drift_order_count": post_drift_orders_by_user[user_id],
            }
        )
    return labels


def sampled_labels(labels: list[dict[str, Any]], limit_per_class: int = 5) -> list[dict[str, Any]]:
    positives = [row for row in labels if row["label"] == 1][:limit_per_class]
    negatives = [row for row in labels if row["label"] == 0][:limit_per_class]
    return positives + negatives


def summarize(config: GeneratorConfig) -> None:
    run_path = Path(config.output.base_path) / config.output.run_id
    feature_path = run_path / "reports/user_daily_features.parquet"
    health_path = run_path / "monitoring/agg_feature_health_daily.parquet"
    if not feature_path.exists() or not health_path.exists():
        raise FileNotFoundError(
            "Missing drift artifacts. Run generator with configs/local/data_generator_drift.yaml first."
        )

    labels = build_label_rows(config, run_path)
    label_by_user = {row["user_id"]: row for row in labels}
    features = pq.read_table(feature_path).to_pylist()
    latest_feature_date = max(row["feature_date"] for row in features)
    latest_features = [
        row for row in features if row["feature_date"] == latest_feature_date
    ]

    positive_users = sum(row["label"] == 1 for row in labels)
    negative_users = sum(row["label"] == 0 for row in labels)

    merged_rows = []
    for feature in latest_features:
        label = label_by_user[feature["user_id"]]
        merged_rows.append(
            {
                "user_id": feature["user_id"],
                "label": label["label"],
                "feature_date": feature["feature_date"],
                "f_user_purchase_count_90d": feature["f_user_purchase_count_90d"],
                "f_user_total_orders_90d": feature["f_user_total_orders_90d"],
                "f_user_interaction_count_90d": feature[
                    "f_user_interaction_count_90d"
                ],
                "feature_version": feature["feature_version"],
            }
        )
    merged_rows.sort(key=lambda row: (-row["label"], row["user_id"]))

    health_rows = pq.read_table(health_path).to_pylist()
    drift_dates = [
        config.drift.baseline_start_date,
        config.drift.baseline_end_date,
        config.drift.drift_start_date,
        (
            config.drift.drift_start_date + timedelta(days=config.drift.ramp_up_days)
            if config.drift.drift_start_date is not None
            else None
        ),
        config.history_start_date + timedelta(days=config.history_days - 1),
    ]
    drift_dates = [value for value in drift_dates if value is not None]
    health_sample = [
        row
        for row in health_rows
        if row["feature_name"] == "f_user_purchase_count_90d"
        and row["date"] in set(drift_dates)
    ]

    print("# Drift Label Merge Evidence")

    print_section("Generator Configuration")
    print_markdown_table(
        ["setting", "value"],
        [
            ["run_id", config.output.run_id],
            ["seed", config.seed],
            ["history_start_date", config.history_start_date],
            ["history_days", config.history_days],
            ["target_behavior_events", config.traffic.target_behavior_events],
            ["drift_enabled", config.drift.enabled],
            ["drift_scenario", config.drift.scenario],
            ["drift_start_date", config.drift.drift_start_date],
            ["drift_mode", config.drift.drift_mode],
            [
                "purchase_probability_multiplier",
                config.drift.purchase_probability_multiplier,
            ],
            ["ramp_up_days", config.drift.ramp_up_days],
            ["baseline_start_date", config.drift.baseline_start_date],
            ["baseline_end_date", config.drift.baseline_end_date],
            ["psi_alert_threshold", config.drift.psi_alert_threshold],
        ],
    )

    print_section("Drift Health Sample")
    print_markdown_table(
        ["date", "feature_name", "mean", "psi_vs_baseline", "drift_status", "drift_factor"],
        [
            [
                row["date"],
                row["feature_name"],
                round(row["mean"], 4),
                round(row["psi_vs_baseline"], 6),
                row["drift_status"],
                round(row["drift_factor"], 4),
            ]
            for row in sorted(health_sample, key=lambda value: value["date"])
        ],
    )

    print_section("Label Table")
    print(f"- label definition: user has >=1 order on/after {config.drift.drift_start_date}")
    print(f"- label distribution: positive={positive_users}, negative={negative_users}")
    print_markdown_table(
        ["user_id", "label"],
        [[row["user_id"], row["label"]] for row in sampled_labels(labels)],
    )

    print_section("Merged Features With Labels")
    print_markdown_table(
        [
            "user_id",
            "label",
            "feature_date",
            "f_user_purchase_count_90d",
            "f_user_total_orders_90d",
            "f_user_interaction_count_90d",
            "feature_version",
        ],
        [
            [
                row["user_id"],
                row["label"],
                row["feature_date"],
                row["f_user_purchase_count_90d"],
                row["f_user_total_orders_90d"],
                row["f_user_interaction_count_90d"],
                row["feature_version"],
            ]
            for row in merged_rows[:12]
        ],
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Print aligned drift config, label table, and feature-label merge proof."
    )
    parser.add_argument("--config", required=True, help="Path to drift generator YAML")
    args = parser.parse_args()
    summarize(load_config(args.config))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
