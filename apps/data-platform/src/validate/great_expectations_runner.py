from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from warehouse.connection import connect
from warehouse.schemas import MONITORING_DATA_QUALITY_RUNS
from warehouse.writer import upsert_rows


@dataclass(frozen=True)
class QualityCheck:
    name: str
    passed: bool
    errors: list[str]
    metrics: dict[str, Any]


STAGING_TABLE_CONTRACTS = {
    "staging.stream_behavior_events": {
        "required_columns": ["event_id", "event_timestamp", "user_id", "product_id", "event_type"],
        "unique_columns": ["event_id"],
        "categorical_columns": ["event_type", "category_id", "brand_id"],
        "freshness_column": "processed_timestamp",
    },
    "staging.stream_user_sequence_features": {
        "required_columns": ["user_id", "feature_timestamp", "sequence_length", "feature_payload"],
        "unique_columns": ["user_id", "feature_timestamp"],
        "categorical_columns": [],
        "freshness_column": "feature_timestamp",
    },
    "staging.stream_user_aggregate_features": {
        "required_columns": ["user_id", "feature_timestamp", "views_30m", "purchases_24h"],
        "unique_columns": ["user_id", "feature_timestamp"],
        "categorical_columns": [],
        "freshness_column": "feature_timestamp",
    },
    "staging.stream_item_features": {
        "required_columns": ["product_id", "feature_timestamp", "views_1h", "popularity_score"],
        "unique_columns": ["product_id", "feature_timestamp"],
        "categorical_columns": ["category_id", "brand_id", "price_bucket"],
        "freshness_column": "feature_timestamp",
    },
}


def _read_table(connection: Any, qualified_name: str) -> pd.DataFrame:
    return pd.read_sql_query(f"SELECT * FROM {qualified_name}", connection)


def _great_expectations_validator(frame: pd.DataFrame):
    try:
        import great_expectations as gx
    except ImportError:
        return None, "not_installed"

    if hasattr(gx, "from_pandas"):
        try:
            return gx.from_pandas(frame), "great_expectations.from_pandas"
        except Exception as exc:
            return None, f"from_pandas_failed:{exc.__class__.__name__}"
    try:
        from great_expectations.dataset import PandasDataset

        return PandasDataset(frame), "great_expectations.dataset.PandasDataset"
    except Exception as exc:
        return None, f"pandas_dataset_failed:{exc.__class__.__name__}"


def _run_expectation(validator: Any, expectation: str, **kwargs: Any) -> tuple[bool, dict[str, Any]]:
    result = getattr(validator, expectation)(**kwargs)
    payload = result.to_json_dict() if hasattr(result, "to_json_dict") else dict(result)
    return bool(payload.get("success", False)), payload


def _validate_with_great_expectations(
    table_name: str,
    frame: pd.DataFrame,
    required_columns: list[str],
    unique_columns: list[str],
    categorical_columns: list[str],
    max_unique_ratio: float,
) -> tuple[list[str], dict[str, Any]]:
    validator, backend = _great_expectations_validator(frame)
    metrics: dict[str, Any] = {
        "great_expectations_backend": backend,
        "great_expectations_used": validator is not None,
        "great_expectations_expectation_count": 0,
        "great_expectations_failed_expectation_count": 0,
    }
    errors: list[str] = []
    if validator is None:
        return errors, metrics

    expectations: list[tuple[str, dict[str, Any], str]] = [
        (
            "expect_table_columns_to_contain_set",
            {"column_set": required_columns},
            "required column set",
        ),
    ]
    expectations.extend(
        (
            "expect_column_values_to_not_be_null",
            {"column": column},
            f"{column} non-null",
        )
        for column in required_columns
        if column in frame.columns
    )
    if len(unique_columns) == 1 and unique_columns[0] in frame.columns:
        expectations.append(
            (
                "expect_column_values_to_be_unique",
                {"column": unique_columns[0]},
                f"{unique_columns[0]} unique",
            )
        )
    elif all(column in frame.columns for column in unique_columns):
        expectations.append(
            (
                "expect_compound_columns_to_be_unique",
                {"column_list": unique_columns},
                f"{unique_columns} compound unique",
            )
        )
    expectations.extend(
        (
            "expect_column_proportion_of_unique_values_to_be_between",
            {"column": column, "max_value": max_unique_ratio},
            f"{column} cardinality",
        )
        for column in categorical_columns
        if column in frame.columns
    )

    for expectation, kwargs, label in expectations:
        if not hasattr(validator, expectation):
            continue
        metrics["great_expectations_expectation_count"] += 1
        try:
            success, payload = _run_expectation(validator, expectation, **kwargs)
        except Exception as exc:
            success = False
            payload = {"exception": exc.__class__.__name__}
        metrics[f"ge_{label.replace(' ', '_').replace('.', '_')}_success"] = success
        if not success:
            metrics["great_expectations_failed_expectation_count"] += 1
            errors.append(f"{table_name} failed GE expectation: {label}")

    return errors, metrics


def validate_table(
    table_name: str,
    frame: pd.DataFrame,
    required_columns: list[str],
    unique_columns: list[str],
    categorical_columns: list[str],
    freshness_column: str,
    max_top_value_ratio: float,
    max_unique_ratio: float,
) -> QualityCheck:
    errors: list[str] = []
    metrics: dict[str, Any] = {"row_count": int(len(frame))}
    ge_errors, ge_metrics = _validate_with_great_expectations(
        table_name,
        frame,
        required_columns=required_columns,
        unique_columns=unique_columns,
        categorical_columns=categorical_columns,
        max_unique_ratio=max_unique_ratio,
    )
    errors.extend(ge_errors)
    metrics.update(ge_metrics)
    missing = sorted(set(required_columns) - set(frame.columns))
    metrics["missing_column_count"] = len(missing)
    if missing:
        errors.append(f"{table_name} missing required columns: {missing}")
        return QualityCheck(table_name, False, errors, metrics)
    if frame.empty:
        errors.append(f"{table_name} is empty")
        return QualityCheck(table_name, False, errors, metrics)

    duplicate_count = int(frame.duplicated(unique_columns).sum())
    metrics["duplicate_count"] = duplicate_count
    if duplicate_count:
        errors.append(f"{table_name} has {duplicate_count} duplicate records at grain {unique_columns}")

    skewed_columns = []
    high_cardinality_columns = []
    for column in categorical_columns:
        if column not in frame.columns:
            continue
        value_counts = frame[column].value_counts(dropna=False)
        top_ratio = float(value_counts.iloc[0] / len(frame)) if len(value_counts) else 0.0
        unique_ratio = float(frame[column].nunique(dropna=False) / len(frame))
        metrics[f"{column}_top_value_ratio"] = top_ratio
        metrics[f"{column}_unique_ratio"] = unique_ratio
        if top_ratio > max_top_value_ratio:
            skewed_columns.append(column)
        if unique_ratio > max_unique_ratio:
            high_cardinality_columns.append(column)
    if skewed_columns:
        errors.append(f"{table_name} skewed categorical columns: {skewed_columns}")
    if high_cardinality_columns:
        errors.append(f"{table_name} high-cardinality categorical columns: {high_cardinality_columns}")

    if freshness_column in frame.columns:
        latest = pd.to_datetime(frame[freshness_column], utc=True).max()
        metrics["latest_timestamp"] = latest.isoformat() if pd.notna(latest) else None

    return QualityCheck(table_name, not errors, errors, metrics)


def write_report(path: str | Path, report: dict[str, Any]) -> str:
    raw_path = str(path)
    if raw_path.startswith("s3://"):
        import boto3

        without_scheme = raw_path.removeprefix("s3://")
        bucket, key = without_scheme.split("/", 1)
        client = boto3.client(
            "s3",
            endpoint_url=os.getenv("MINIO_ENDPOINT", "http://minio:9000"),
            aws_access_key_id=os.getenv("MINIO_ROOT_USER", os.getenv("AWS_ACCESS_KEY_ID", "minio")),
            aws_secret_access_key=os.getenv("MINIO_ROOT_PASSWORD", os.getenv("AWS_SECRET_ACCESS_KEY", "minio123")),
            region_name=os.getenv("AWS_DEFAULT_REGION", "us-east-1"),
        )
        client.put_object(
            Bucket=bucket,
            Key=key,
            Body=json.dumps(report, indent=2, sort_keys=True, default=str).encode("utf-8"),
            ContentType="application/json",
        )
        return raw_path
    output = Path(raw_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, sort_keys=True, default=str), encoding="utf-8")
    return str(output)


def run_staging_validation(
    run_id: str,
    report_path: str,
    max_top_value_ratio: float = 0.95,
    max_unique_ratio: float = 0.98,
) -> dict[str, Any]:
    checks: list[QualityCheck] = []
    with connect() as connection:
        for table_name, contract in STAGING_TABLE_CONTRACTS.items():
            frame = _read_table(connection, table_name)
            checks.append(
                validate_table(
                    table_name,
                    frame,
                    required_columns=contract["required_columns"],
                    unique_columns=contract["unique_columns"],
                    categorical_columns=contract["categorical_columns"],
                    freshness_column=contract["freshness_column"],
                    max_top_value_ratio=max_top_value_ratio,
                    max_unique_ratio=max_unique_ratio,
                )
            )
        monitoring_rows = [
            {
                "run_id": run_id,
                "check_name": check.name,
                "passed": check.passed,
                "error_count": len(check.errors),
                "metrics": check.metrics,
                "created_timestamp": datetime.now(timezone.utc),
            }
            for check in checks
        ]
        upsert_rows(connection, MONITORING_DATA_QUALITY_RUNS, monitoring_rows)

    report = {
        "run_id": run_id,
        "passed": all(check.passed for check in checks),
        "checks": [check.__dict__ for check in checks],
    }
    report["report_path"] = write_report(report_path, report)
    if not report["passed"]:
        raise SystemExit(json.dumps(report, indent=2, sort_keys=True, default=str))
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Great Expectations staging data validation.")
    parser.add_argument("--run-id", default=os.getenv("DATA_QUALITY_RUN_ID", datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")))
    parser.add_argument(
        "--report-path",
        default=os.getenv(
            "GE_REPORT_PATH",
            "s3://recsys-lake/monitoring/great_expectations/staging_validation.json",
        ),
    )
    parser.add_argument("--max-top-value-ratio", type=float, default=float(os.getenv("GE_MAX_TOP_VALUE_RATIO", "0.95")))
    parser.add_argument("--max-unique-ratio", type=float, default=float(os.getenv("GE_MAX_UNIQUE_RATIO", "0.98")))
    args = parser.parse_args()
    print(
        json.dumps(
            run_staging_validation(
                args.run_id,
                args.report_path,
                max_top_value_ratio=args.max_top_value_ratio,
                max_unique_ratio=args.max_unique_ratio,
            ),
            indent=2,
            sort_keys=True,
            default=str,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
