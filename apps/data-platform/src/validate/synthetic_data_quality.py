from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from ingest.minio_raw_reader import read_generator_table, s3_storage_options
from validate.great_expectations_runner import QualityCheck, validate_table, write_report
from warehouse.connection import connect
from warehouse.schemas import MONITORING_DATA_QUALITY_RUNS
from warehouse.writer import ensure_warehouse, upsert_rows


QUALITY_CHECK_NAME = "generator.offline.synthetic_issues"


def _read_json(path: str | Path) -> dict[str, Any]:
    raw = str(path)
    if raw.startswith("s3://"):
        import s3fs

        without_scheme = raw.removeprefix("s3://")
        filesystem = s3fs.S3FileSystem(anon=False, **s3_storage_options())
        with filesystem.open(without_scheme, "r") as handle:
            return json.load(handle)
    with Path(raw).open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _safe_ratio(numerator: float, denominator: float) -> float:
    return round(float(numerator) / denominator, 6) if denominator else 0.0


def _value_distribution(
    frame: pd.DataFrame, column: str, limit: int = 10
) -> dict[str, float]:
    if column not in frame.columns or frame.empty:
        return {}
    counts = frame[column].value_counts(dropna=False).head(limit)
    return {
        str(value).lower().replace(" ", "_"): _safe_ratio(count, len(frame))
        for value, count in counts.items()
    }


def _table_metrics(run_path: str | Path) -> dict[str, Any]:
    metrics: dict[str, Any] = {}
    table_ids = {
        "users": "user_id",
        "products": "product_id",
        "sessions": "session_id",
        "recommendation_requests": "request_id",
        "impressions": "impression_id",
        "behavior_events": "event_id",
        "orders": "order_id",
    }
    for table_name, id_column in table_ids.items():
        frame = read_generator_table(run_path, table_name)
        metrics[f"approx_count_distinct_{table_name}_{id_column}"] = int(
            frame[id_column].nunique(dropna=True)
        )
        metrics[f"row_count_from_parquet_{table_name}"] = int(len(frame))
        if table_name == "users":
            metrics["city_distribution"] = _value_distribution(frame, "city")
        if table_name == "behavior_events":
            metrics["event_category_distribution"] = _value_distribution(
                frame, "category_id"
            )
    return metrics


def _offline_great_expectations_checks(run_path: str | Path) -> list[QualityCheck]:
    users = read_generator_table(run_path, "users")
    products = read_generator_table(run_path, "products")
    behavior_events = read_generator_table(run_path, "behavior_events")
    return [
        validate_table(
            "generator.users",
            users,
            required_columns=["user_id", "city"],
            unique_columns=["user_id"],
            categorical_columns=["city"],
            freshness_column="created_ts" if "created_ts" in users.columns else "user_id",
            max_top_value_ratio=0.95,
            max_unique_ratio=0.98,
        ),
        validate_table(
            "generator.products",
            products,
            required_columns=["product_id", "category_id", "brand_id"],
            unique_columns=["product_id"],
            categorical_columns=["category_id", "brand_id"],
            freshness_column="created_ts" if "created_ts" in products.columns else "product_id",
            max_top_value_ratio=0.95,
            max_unique_ratio=0.98,
        ),
        validate_table(
            "generator.behavior_events",
            behavior_events,
            required_columns=["event_id", "event_timestamp", "user_id", "product_id", "event_type"],
            unique_columns=["event_id"],
            categorical_columns=["event_type", "category_id"],
            freshness_column="event_timestamp",
            max_top_value_ratio=0.95,
            max_unique_ratio=0.98,
        ),
    ]


def build_quality_metrics(
    manifest: dict[str, Any],
    dq_report: dict[str, Any],
    table_metrics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    config = manifest.get("config", {})
    entities = config.get("entities", {})
    traffic = config.get("traffic", {})
    distribution = config.get("distribution", {})
    challenges = config.get("challenges", {})
    row_counts = manifest.get("row_counts", {})
    observed = dq_report.get("observed", {})
    injected = dq_report.get("injected", {})
    validation_metrics = dq_report.get("validation_metrics", {})

    raw_events = int(row_counts.get("behavior_events", 0))
    canonical_events = int(validation_metrics.get("canonical_event_count", 0))
    duplicate_rows = max(raw_events - canonical_events, 0)

    metrics: dict[str, Any] = {
        "validation_passed": bool(dq_report.get("validation_passed", False)),
        "configured_n_users": int(entities.get("n_users", 0)),
        "configured_n_products": int(entities.get("n_products", 0)),
        "configured_n_categories": int(entities.get("n_categories", 0)),
        "configured_n_brands": int(entities.get("n_brands", 0)),
        "configured_target_behavior_events": int(
            traffic.get("target_behavior_events", 0)
        ),
        "configured_duplicate_event_rate": float(
            challenges.get("duplicate_event_rate", 0)
        ),
        "configured_conflicting_duplicate_rate": float(
            challenges.get("conflicting_duplicate_rate", 0)
        ),
        "configured_late_arrival_rate": float(challenges.get("late_arrival_rate", 0)),
        "configured_out_of_order_rate": float(challenges.get("out_of_order_rate", 0)),
        "configured_top_city_ratio": float(distribution.get("top_city_ratio", 0)),
        "configured_top_category_ratio": float(
            distribution.get("top_category_ratio", 0)
        ),
        "configured_burst_window_count": len(config.get("burst_windows", [])),
        "raw_behavior_event_rows": raw_events,
        "canonical_event_count": canonical_events,
        "duplicate_rows_before_dedup": duplicate_rows,
        "duplicate_rate_before_dedup": _safe_ratio(duplicate_rows, raw_events),
        "duplicate_rows_after_dedup": 0,
        "duplicate_rate_after_dedup": 0.0,
        "exact_duplicate_rows": int(observed.get("exact_duplicate_rows", 0)),
        "conflicting_duplicate_event_ids": int(
            observed.get("conflicting_duplicate_event_ids", 0)
        ),
        "late_arrival_rate": float(observed.get("late_arrival_rate", 0)),
        "out_of_order_rate": float(observed.get("out_of_order_rate", 0)),
        "schema_v1_events": int(observed.get("schema_v1_events", 0)),
        "schema_v2_events": int(observed.get("schema_v2_events", 0)),
        "null_device_type_events": int(observed.get("null_device_type_events", 0)),
        "top_city_ratio": float(observed.get("top_city_ratio", 0)),
        "top_event_category_ratio": float(
            observed.get("top_event_category_ratio", 0)
        ),
        "injected_exact_duplicates": int(injected.get("exact_duplicates", 0)),
        "injected_conflicting_duplicates": int(
            injected.get("conflicting_duplicates", 0)
        ),
        "injected_late_arrivals": int(injected.get("late_arrivals", 0)),
        "injected_out_of_order": int(injected.get("out_of_order", 0)),
    }
    metrics.update(
        {f"row_count_{name}": int(count) for name, count in row_counts.items()}
    )
    if table_metrics:
        metrics.update(table_metrics)
    return metrics


def publish_synthetic_quality(
    run_path: str,
    run_id: str | None = None,
    report_path: str | None = None,
    write_monitoring: bool = True,
) -> dict[str, Any]:
    normalized_run_path = run_path.rstrip("/")
    manifest = _read_json(f"{normalized_run_path}/manifest.json")
    dq_report = _read_json(f"{normalized_run_path}/data_quality_report.json")
    actual_run_id = run_id or manifest.get("run_id") or Path(normalized_run_path).name

    errors = list(dq_report.get("validation_errors", []))
    try:
        parquet_metrics = _table_metrics(normalized_run_path)
    except Exception as exc:
        parquet_metrics = {"parquet_metric_collection_failed": 1}
        errors.append(f"parquet metric collection failed: {exc.__class__.__name__}")

    try:
        ge_checks = _offline_great_expectations_checks(normalized_run_path)
    except Exception as exc:
        ge_checks = []
        errors.append(f"great expectations offline checks failed: {exc.__class__.__name__}")

    metrics = build_quality_metrics(manifest, dq_report, parquet_metrics)
    metrics["great_expectations_offline_check_count"] = len(ge_checks)
    metrics["great_expectations_offline_failed_check_count"] = sum(
        1 for check in ge_checks if not check.passed
    )
    metrics["great_expectations_offline_used"] = any(
        bool(check.metrics.get("great_expectations_used")) for check in ge_checks
    )
    for check in ge_checks:
        for metric_name, value in check.metrics.items():
            safe_name = check.name.replace(".", "_")
            metrics[f"{safe_name}_{metric_name}"] = value
        errors.extend(check.errors)
    passed = bool(dq_report.get("validation_passed", False)) and not errors
    monitoring_row = {
        "run_id": str(actual_run_id),
        "check_name": QUALITY_CHECK_NAME,
        "passed": passed,
        "error_count": len(errors),
        "metrics": metrics,
        "created_timestamp": datetime.now(timezone.utc),
    }
    if write_monitoring:
        with connect() as connection:
            ensure_warehouse(connection)
            upsert_rows(connection, MONITORING_DATA_QUALITY_RUNS, [monitoring_row])

    report = {
        "run_id": actual_run_id,
        "check_name": QUALITY_CHECK_NAME,
        "passed": passed,
        "errors": errors,
        "metrics": metrics,
        "great_expectations_checks": [check.__dict__ for check in ge_checks],
    }
    if report_path:
        report["report_path"] = write_report(report_path, report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Publish synthetic generator data issues to monitoring tables."
    )
    parser.add_argument(
        "--run-path",
        default=os.getenv("GENERATOR_RUN_PATH", "s3://recsys-lake/raw/test_10k_seed42"),
    )
    parser.add_argument("--run-id", default=os.getenv("GENERATOR_RUN_ID"))
    parser.add_argument(
        "--report-path",
        default=os.getenv(
            "GENERATOR_QUALITY_REPORT_PATH",
            "s3://recsys-lake/monitoring/generator/offline_quality.json",
        ),
    )
    parser.add_argument("--skip-monitoring-write", action="store_true")
    args = parser.parse_args()
    print(
        json.dumps(
            publish_synthetic_quality(
                args.run_path,
                args.run_id,
                args.report_path,
                write_monitoring=not args.skip_monitoring_write,
            ),
            indent=2,
            sort_keys=True,
            default=str,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
