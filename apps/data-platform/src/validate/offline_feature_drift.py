from __future__ import annotations

import argparse
import json
import math
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import pandas as pd
import pyarrow as pa
import pyarrow.fs as pafs
import pyarrow.parquet as pq

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
    feature_view: str
    feature: str
    drift_score: float
    passed: bool
    reference_rows: int
    current_rows: int
    threshold: float
    metric: str = "psi"


class MissingFeatureDataError(FileNotFoundError):
    pass


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


def _normalise_uri(uri: str | Path) -> str:
    value = str(uri)
    if value.startswith("s3a://"):
        return "s3://" + value.removeprefix("s3a://")
    return value


def _s3_endpoint() -> tuple[str, str]:
    endpoint = os.getenv("MINIO_ENDPOINT", os.getenv("DATA_PLATFORM_MINIO_ENDPOINT", "http://data-platform-minio:9000"))
    parsed = urlparse(endpoint)
    if not parsed.scheme:
        return "http", endpoint
    return parsed.scheme, parsed.netloc


def _filesystem_and_path(uri: str | Path) -> tuple[pafs.FileSystem, str]:
    normalised = _normalise_uri(uri)
    parsed = urlparse(normalised)
    if parsed.scheme == "s3":
        scheme, endpoint = _s3_endpoint()
        return (
            pafs.S3FileSystem(
                access_key=os.getenv("AWS_ACCESS_KEY_ID", os.getenv("MINIO_ROOT_USER", "minio")),
                secret_key=os.getenv("AWS_SECRET_ACCESS_KEY", os.getenv("MINIO_ROOT_PASSWORD", "minio123")),
                region=os.getenv("AWS_DEFAULT_REGION", "us-east-1"),
                scheme=scheme,
                endpoint_override=endpoint,
            ),
            f"{parsed.netloc}{parsed.path}",
        )
    if parsed.scheme == "file":
        return pafs.LocalFileSystem(), parsed.path
    if parsed.scheme:
        raise ValueError(f"Unsupported feature drift URI scheme: {parsed.scheme}")
    return pafs.LocalFileSystem(), str(Path(normalised))


def _parquet_files(uri: str | Path) -> tuple[pafs.FileSystem, list[str]]:
    filesystem, path = _filesystem_and_path(uri)
    info = filesystem.get_file_info(path)
    if info.type == pafs.FileType.File and path.endswith(".parquet"):
        return filesystem, [path]
    if info.type == pafs.FileType.NotFound:
        raise MissingFeatureDataError(f"No parquet data found at {uri}")

    selector = pafs.FileSelector(path, recursive=True)
    files = sorted(
        item.path
        for item in filesystem.get_file_info(selector)
        if item.type == pafs.FileType.File and item.path.endswith(".parquet")
    )
    if not files:
        raise MissingFeatureDataError(f"No parquet data found at {uri}")
    return filesystem, files


def _read_parquet_frame(uri: str | Path) -> pd.DataFrame:
    filesystem, files = _parquet_files(uri)
    return pq.read_table(files, filesystem=filesystem).to_pandas()


def _delete_dir_if_exists(filesystem: pafs.FileSystem, path: str) -> None:
    try:
        filesystem.delete_dir(path)
    except FileNotFoundError:
        return
    except OSError:
        return


def _write_parquet_frame(frame: pd.DataFrame, uri: str | Path, *, run_id: str) -> None:
    filesystem, path = _filesystem_and_path(uri)
    _delete_dir_if_exists(filesystem, path)
    filesystem.create_dir(path, recursive=True)
    safe_run_id = "".join(char if char.isalnum() or char in "._-" else "-" for char in run_id).strip("-") or "run"
    pq.write_table(pa.Table.from_pandas(frame, preserve_index=False), f"{path.rstrip('/')}/part-{safe_run_id}.parquet", filesystem=filesystem)


def _logical_table_name(table: str) -> str:
    return table.rstrip("/").split("/")[-1].split(".")[-1]


def _table_uri(root_uri: str, table: str) -> str:
    if table.startswith(("s3://", "s3a://", "file://", "/")):
        return table
    return f"{root_uri.rstrip('/')}/{_logical_table_name(table)}"


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "y", "on"}


def _env_list(name: str) -> list[str] | None:
    value = os.getenv(name, "")
    values = [item.strip() for item in value.split(",") if item.strip()]
    return values or None


def numeric_feature_columns(frame: Any) -> list[str]:
    if isinstance(frame, pd.DataFrame):
        numeric_columns = frame.select_dtypes(include=["number", "bool"]).columns
        return [
            column
            for column in numeric_columns
            if column not in ID_COLUMNS and not column.endswith("_id") and not column.startswith("__")
        ]

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


def sample_current_frame(frame: pd.DataFrame, *, current_days: int, sample_rows: int, random_state: int) -> pd.DataFrame:
    current = frame
    column = timestamp_column(frame)
    if column:
        timestamps = pd.to_datetime(frame[column], utc=True, errors="coerce")
        max_ts = timestamps.max()
        if pd.notna(max_ts):
            cutoff = max_ts - timedelta(days=current_days)
            windowed = frame[timestamps > cutoff]
            if not windowed.empty:
                current = windowed
    if sample_rows > 0 and len(current) > sample_rows:
        return current.sample(n=sample_rows, random_state=random_state).reset_index(drop=True)
    return current.reset_index(drop=True)


def _numeric_values(frame: pd.DataFrame, column: str) -> list[float]:
    values = pd.to_numeric(frame[column], errors="coerce").dropna()
    return [float(value) for value in values if math.isfinite(float(value))]


def analyze_feature_table(
    reference: pd.DataFrame,
    current: pd.DataFrame,
    table_name: str,
    threshold: float,
) -> list[DriftFeatureResult]:
    reference_rows = int(len(reference))
    current_rows = int(len(current))
    features = sorted(set(numeric_feature_columns(reference)).intersection(numeric_feature_columns(current)))
    results: list[DriftFeatureResult] = []
    for feature in features:
        score = calculate_psi(_numeric_values(reference, feature), _numeric_values(current, feature))
        results.append(
            DriftFeatureResult(
                feature_table=table_name,
                feature_view=table_name,
                feature=feature,
                drift_score=score,
                passed=score < threshold,
                reference_rows=reference_rows,
                current_rows=current_rows,
                threshold=threshold,
            )
        )
    return results


def _jsonable(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str))


def run_evidently_report(reference: pd.DataFrame, current: pd.DataFrame) -> dict[str, Any]:
    columns = sorted(set(numeric_feature_columns(reference)).intersection(numeric_feature_columns(current)))
    reference_data = reference[columns] if columns else reference
    current_data = current[columns] if columns else current
    try:
        try:
            from evidently.report import Report
            from evidently.metric_preset import DataDriftPreset

            report = Report(metrics=[DataDriftPreset()])
            report.run(reference_data=reference_data, current_data=current_data)
            return {"engine": "evidently", "status": "ok", "report": _jsonable(report.as_dict())}
        except ModuleNotFoundError:
            from evidently import Report
            from evidently.presets import DataDriftPreset

            report = Report([DataDriftPreset()])
            result = report.run(reference_data=reference_data, current_data=current_data)
            if hasattr(result, "dict"):
                payload = result.dict()
            elif hasattr(result, "model_dump"):
                payload = result.model_dump()
            else:
                payload = result
            return {"engine": "evidently", "status": "ok", "report": _jsonable(payload)}
    except ModuleNotFoundError as exc:
        return {"engine": "psi", "status": "evidently_unavailable", "error": str(exc)}
    except Exception as exc:
        return {"engine": "psi", "status": "evidently_error", "error": str(exc)}


def metric_samples(run_id: str, results: list[DriftFeatureResult]) -> list[MetricSample]:
    samples = [
        MetricSample("recsys_ml_feature_drift_run_timestamp_seconds", datetime.now(timezone.utc).timestamp(), {"run_id": run_id})
    ]
    row_metric_tables: set[str] = set()
    for result in results:
        labels = {"feature_view": result.feature_table, "feature": result.feature}
        samples.extend(
            [
                MetricSample("recsys_ml_feature_drift_psi", result.drift_score, labels),
                MetricSample("recsys_ml_feature_drift_passed", 1.0 if result.passed else 0.0, labels),
            ]
        )
        if result.feature_table not in row_metric_tables:
            row_metric_tables.add(result.feature_table)
            samples.extend(
                [
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
    current_feature_root: str | None = None,
    baseline_path: str | None = None,
    sample_rows: int = 1000,
    bootstrap_baseline: bool = True,
    random_state: int = 42,
) -> dict[str, Any]:
    current_root = current_feature_root or os.getenv(
        "OFFLINE_FEATURE_DRIFT_CURRENT_ROOT",
        os.getenv("OFFLINE_FEATURE_STORE_URI", "s3a://recsys-offline-feature-store/warehouse/feature_store"),
    )
    reference_root = baseline_path or os.getenv(
        "OFFLINE_FEATURE_DRIFT_BASELINE_PATH",
        "s3://recsys-offline-feature-store/monitoring/offline_feature_drift/reference_baseline",
    )
    results: list[DriftFeatureResult] = []
    errors: list[str] = []
    bootstrapped_baselines: list[str] = []
    tables: dict[str, Any] = {}
    evidently_reports: dict[str, Any] = {}

    for table in feature_tables or DEFAULT_FEATURE_TABLES:
        table_name = _logical_table_name(table)
        current_uri = _table_uri(current_root, table)
        reference_uri = _table_uri(reference_root, table_name)
        try:
            current_full = _read_parquet_frame(current_uri)
            current = sample_current_frame(
                current_full,
                current_days=current_days,
                sample_rows=sample_rows,
                random_state=random_state,
            )
            try:
                reference = _read_parquet_frame(reference_uri)
            except MissingFeatureDataError:
                if not bootstrap_baseline:
                    raise
                _write_parquet_frame(current, reference_uri, run_id=run_id)
                bootstrapped_baselines.append(table_name)
                tables[table_name] = {
                    "current_uri": current_uri,
                    "baseline_uri": reference_uri,
                    "current_rows": int(len(current)),
                    "reference_rows": int(len(current)),
                    "baseline_bootstrapped": True,
                }
                continue

            evidently_reports[table_name] = run_evidently_report(reference, current)
            results.extend(analyze_feature_table(reference, current, table_name, threshold))
            tables[table_name] = {
                "current_uri": current_uri,
                "baseline_uri": reference_uri,
                "current_rows": int(len(current)),
                "reference_rows": int(len(reference)),
                "baseline_bootstrapped": False,
            }
        except Exception as exc:
            errors.append(f"{table_name}: {exc}")

    passed = not errors and (all(result.passed for result in results) if results else bool(bootstrapped_baselines))
    engines = {payload.get("engine", "psi") for payload in evidently_reports.values()}
    drift_engine = "evidently" if "evidently" in engines else "psi"
    report = {
        "run_id": run_id,
        "drift_kind": "feature_drift",
        "drift_engine": drift_engine,
        "groundtruth_available": False,
        "passed": passed,
        "features": [asdict(result) for result in results],
        "tables": tables,
        "evidently": evidently_reports,
        "baseline_bootstrapped": bootstrapped_baselines,
        "sample_rows": sample_rows,
        "current_days": current_days,
        "errors": errors,
    }
    report["report_path"] = write_report(report_path, report)
    push_metrics(
        metric_samples(run_id, results),
        job="recsys_offline_feature_drift",
        gateway_url=pushgateway_url,
        grouping_key={"run_id": run_id},
    )
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Python/Evidently offline feature drift monitoring and push metrics.")
    parser.add_argument("--run-id", default=os.getenv("FEATURE_DRIFT_RUN_ID", datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")))
    parser.add_argument(
        "--report-path",
        default=os.getenv(
            "OFFLINE_FEATURE_DRIFT_REPORT_PATH",
            "s3://recsys-offline-feature-store/monitoring/offline_feature_drift/report.json",
        ),
    )
    parser.add_argument("--current-feature-root", default=os.getenv("OFFLINE_FEATURE_DRIFT_CURRENT_ROOT"))
    parser.add_argument("--baseline-path", default=os.getenv("OFFLINE_FEATURE_DRIFT_BASELINE_PATH"))
    parser.add_argument("--feature-table", action="append", dest="feature_tables")
    parser.add_argument("--threshold", type=float, default=float(os.getenv("RETRAIN_PSI_THRESHOLD", "0.15")))
    parser.add_argument(
        "--current-days",
        type=int,
        default=int(os.getenv("OFFLINE_FEATURE_DRIFT_CURRENT_DAYS", os.getenv("FEATURE_DRIFT_CURRENT_DAYS", "7"))),
    )
    parser.add_argument("--sample-rows", type=int, default=int(os.getenv("OFFLINE_FEATURE_DRIFT_SAMPLE_ROWS", "1000")))
    parser.add_argument(
        "--bootstrap-baseline",
        action=argparse.BooleanOptionalAction,
        default=_env_bool("OFFLINE_FEATURE_DRIFT_BOOTSTRAP_BASELINE", True),
    )
    parser.add_argument("--pushgateway-url", default=os.getenv("PUSHGATEWAY_URL", ""))
    parser.add_argument("--fail-on-drift", action="store_true")
    args = parser.parse_args()
    report = run_offline_feature_drift(
        args.run_id,
        args.report_path,
        feature_tables=args.feature_tables or _env_list("OFFLINE_FEATURE_DRIFT_TABLES"),
        threshold=args.threshold,
        current_days=args.current_days,
        pushgateway_url=args.pushgateway_url or None,
        current_feature_root=args.current_feature_root,
        baseline_path=args.baseline_path,
        sample_rows=args.sample_rows,
        bootstrap_baseline=args.bootstrap_baseline,
    )
    print(json.dumps(report, indent=2, sort_keys=True, default=str))
    return 1 if args.fail_on_drift and not report["passed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
