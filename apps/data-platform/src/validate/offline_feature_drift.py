from __future__ import annotations

import argparse
import json
import math
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from feature_engineering.spark.session import spark_session
from lakehouse.iceberg import IcebergCatalogConfig
from monitoring.pushgateway import MetricSample, push_metrics


DEFAULT_FEATURE_TABLES = [
    "user_aggregate_features",
    "item_features",
    "ml_bst_training",
]
ID_COLUMNS = {
    "user_id",
    "product_id",
    "candidate_product_id",
    "event_type_id",
    "event_id",
    "session_id",
    "request_id",
    "order_id",
}
TIMESTAMP_COLUMNS = (
    "feature_timestamp",
    "event_timestamp",
    "prediction_timestamp",
    "created_timestamp",
    "updated_at",
)
NUMERIC_TYPE_PREFIXES = (
    "bigint",
    "decimal",
    "double",
    "float",
    "int",
    "long",
    "short",
)


@dataclass(frozen=True)
class DriftFeatureResult:
    feature_table: str
    feature: str
    drift_score: float
    passed: bool
    reference_rows: int
    current_rows: int
    threshold: float


def _quantile(sorted_values: list[float], q: float) -> float:
    if not sorted_values:
        return 0.0
    position = (len(sorted_values) - 1) * q
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return sorted_values[int(position)]
    weight = position - lower
    return sorted_values[lower] * (1 - weight) + sorted_values[upper] * weight


def calculate_psi(expected: list[float], actual: list[float], buckets: int = 10) -> float:
    expected_values = sorted(float(value) for value in expected if value is not None and math.isfinite(float(value)))
    actual_values = sorted(float(value) for value in actual if value is not None and math.isfinite(float(value)))
    if not expected_values or not actual_values:
        return 0.0

    boundaries = sorted({_quantile(expected_values, index / buckets) for index in range(buckets + 1)})
    if len(boundaries) < 3:
        minimum = min(expected_values[0], actual_values[0])
        maximum = max(expected_values[-1], actual_values[-1])
        if minimum == maximum:
            return 0.0
        boundaries = [minimum, (minimum + maximum) / 2.0, maximum]

    expected_counts = _histogram(expected_values, boundaries)
    actual_counts = _histogram(actual_values, boundaries)
    epsilon = 1e-4
    expected_total = max(sum(expected_counts), 1)
    actual_total = max(sum(actual_counts), 1)
    psi = 0.0
    for expected_count, actual_count in zip(expected_counts, actual_counts, strict=True):
        expected_pct = max(expected_count / expected_total, epsilon)
        actual_pct = max(actual_count / actual_total, epsilon)
        psi += (actual_pct - expected_pct) * math.log(actual_pct / expected_pct)
    return float(psi)


def _histogram(values: list[float], boundaries: list[float]) -> list[int]:
    counts = [0 for _ in range(len(boundaries) - 1)]
    for value in values:
        for index in range(len(boundaries) - 1):
            left = boundaries[index]
            right = boundaries[index + 1]
            if (index == len(boundaries) - 2 and left <= value <= right) or left <= value < right:
                counts[index] += 1
                break
    return counts


def numeric_feature_columns(frame: Any) -> list[str]:
    columns: list[str] = []
    for field in frame.schema.fields:
        data_type = field.dataType.simpleString().lower()
        if field.name in ID_COLUMNS or field.name.endswith("_id"):
            continue
        if data_type.startswith(NUMERIC_TYPE_PREFIXES):
            columns.append(field.name)
    return columns


def timestamp_column(frame: Any) -> str | None:
    for column in TIMESTAMP_COLUMNS:
        if column in frame.columns:
            return column
    return None


def split_reference_current(frame: Any, current_days: int) -> tuple[Any, Any]:
    from pyspark.sql import functions as F

    column = timestamp_column(frame)
    if not column:
        indexed = frame.withColumn("__drift_row_number", F.monotonically_increasing_id())
        midpoint = indexed.count() // 2
        return indexed.filter(F.col("__drift_row_number") < midpoint), indexed.filter(F.col("__drift_row_number") >= midpoint)
    normalized = frame.withColumn("__drift_ts", F.to_timestamp(F.col(column)))
    max_ts = normalized.select(F.max("__drift_ts").alias("max_ts")).collect()[0]["max_ts"]
    if max_ts is None:
        midpoint = normalized.count() // 2
        indexed = normalized.withColumn("__drift_row_number", F.monotonically_increasing_id())
        return indexed.filter(F.col("__drift_row_number") < midpoint), indexed.filter(F.col("__drift_row_number") >= midpoint)
    cutoff = max_ts - timedelta(days=current_days)
    reference = normalized.filter(F.col("__drift_ts") <= F.lit(cutoff))
    current = normalized.filter(F.col("__drift_ts") > F.lit(cutoff))
    if reference.limit(1).count() == 0 or current.limit(1).count() == 0:
        indexed = normalized.withColumn("__drift_row_number", F.monotonically_increasing_id())
        midpoint = indexed.count() // 2
        reference = indexed.filter(F.col("__drift_row_number") < midpoint)
        current = indexed.filter(F.col("__drift_row_number") >= midpoint)
    return reference, current


def collect_numeric(frame: Any, column: str) -> list[float]:
    from pyspark.sql import functions as F

    return [
        float(row[column])
        for row in frame.select(F.col(column).cast("double").alias(column)).where(F.col(column).isNotNull()).collect()
        if row[column] is not None
    ]


def analyze_feature_table(frame: Any, table_name: str, threshold: float, current_days: int) -> list[DriftFeatureResult]:
    reference, current = split_reference_current(frame, current_days)
    reference_rows = int(reference.count())
    current_rows = int(current.count())
    results: list[DriftFeatureResult] = []
    for feature in numeric_feature_columns(frame):
        score = calculate_psi(collect_numeric(reference, feature), collect_numeric(current, feature))
        results.append(
            DriftFeatureResult(
                feature_table=table_name,
                feature=feature,
                drift_score=score,
                passed=score < threshold,
                reference_rows=reference_rows,
                current_rows=current_rows,
                threshold=threshold,
            )
        )
    return results


def metric_samples(run_id: str, results: list[DriftFeatureResult]) -> list[MetricSample]:
    samples = [
        MetricSample("recsys_ml_feature_drift_run_timestamp_seconds", datetime.now(timezone.utc).timestamp(), {"run_id": run_id})
    ]
    for result in results:
        labels = {"feature_view": result.feature_table, "feature": result.feature}
        samples.extend(
            [
                MetricSample("recsys_ml_feature_drift_psi", result.drift_score, labels),
                MetricSample("recsys_ml_feature_drift_passed", 1.0 if result.passed else 0.0, labels),
                MetricSample("recsys_ml_feature_drift_reference_rows", result.reference_rows, {"feature_view": result.feature_table}),
                MetricSample("recsys_ml_feature_drift_current_rows", result.current_rows, {"feature_view": result.feature_table}),
            ]
        )
    return samples


def write_report(path: str, report: dict[str, Any]) -> str:
    payload = json.dumps(report, indent=2, sort_keys=True, default=str).encode("utf-8")
    if path.startswith("s3://"):
        import boto3

        bucket, key = path.removeprefix("s3://").split("/", 1)
        client = boto3.client(
            "s3",
            endpoint_url=os.getenv("MINIO_ENDPOINT", "http://data-platform-minio:9000"),
            aws_access_key_id=os.getenv("MINIO_ROOT_USER", os.getenv("AWS_ACCESS_KEY_ID", "minio")),
            aws_secret_access_key=os.getenv("MINIO_ROOT_PASSWORD", os.getenv("AWS_SECRET_ACCESS_KEY", "minio123")),
            region_name=os.getenv("AWS_DEFAULT_REGION", "us-east-1"),
        )
        client.put_object(Bucket=bucket, Key=key, Body=payload, ContentType="application/json")
        return path
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)
    return str(target)


def run_offline_feature_drift(
    run_id: str,
    report_path: str,
    feature_tables: list[str] | None = None,
    threshold: float = 0.15,
    current_days: int = 7,
    pushgateway_url: str | None = None,
) -> dict[str, Any]:
    spark = spark_session("recsys-offline-feature-drift")
    catalog = IcebergCatalogConfig()
    results: list[DriftFeatureResult] = []
    errors: list[str] = []
    try:
        for table in feature_tables or DEFAULT_FEATURE_TABLES:
            table_name = table if "." in table else catalog.feature_table(table)
            try:
                results.extend(analyze_feature_table(spark.table(table_name), table_name.split(".")[-1], threshold, current_days))
            except Exception as exc:
                errors.append(f"{table_name}: {exc}")
    finally:
        spark.stop()

    push_metrics(
        metric_samples(run_id, results),
        job="recsys_offline_feature_drift",
        gateway_url=pushgateway_url,
        grouping_key={"run_id": run_id},
    )
    report = {
        "run_id": run_id,
        "drift_kind": "feature_drift",
        "groundtruth_available": False,
        "passed": bool(results) and all(result.passed for result in results) and not errors,
        "features": [asdict(result) for result in results],
        "errors": errors,
    }
    report["report_path"] = write_report(report_path, report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Iceberg offline feature drift monitoring and push metrics.")
    parser.add_argument("--run-id", default=os.getenv("FEATURE_DRIFT_RUN_ID", datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")))
    parser.add_argument("--report-path", default=os.getenv("OFFLINE_FEATURE_DRIFT_REPORT_PATH", "s3://recsys-offline-feature-store/monitoring/offline_feature_drift/report.json"))
    parser.add_argument("--feature-table", action="append", dest="feature_tables")
    parser.add_argument("--threshold", type=float, default=float(os.getenv("RETRAIN_PSI_THRESHOLD", "0.15")))
    parser.add_argument("--current-days", type=int, default=int(os.getenv("FEATURE_DRIFT_CURRENT_DAYS", "7")))
    parser.add_argument("--pushgateway-url", default=os.getenv("PUSHGATEWAY_URL", ""))
    parser.add_argument("--fail-on-drift", action="store_true")
    args = parser.parse_args()
    report = run_offline_feature_drift(
        args.run_id,
        args.report_path,
        feature_tables=args.feature_tables,
        threshold=args.threshold,
        current_days=args.current_days,
        pushgateway_url=args.pushgateway_url or None,
    )
    print(json.dumps(report, indent=2, sort_keys=True, default=str))
    return 1 if args.fail_on_drift and not report["passed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
