from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from feature_store.offline_writer import read_feature_table
from monitoring.pushgateway import MetricSample, push_metrics
from validate.evidently_feature_drift import calculate_psi
from validate.great_expectations_runner import write_report
from warehouse.connection import connect
from warehouse.schemas import MONITORING_FEATURE_DRIFT_RUNS
from warehouse.writer import upsert_rows


DEFAULT_FEATURE_VIEWS = [
    "user_sequence_features",
    "user_aggregate_features",
    "item_features",
]
ID_COLUMNS = {
    "user_id",
    "product_id",
    "event_timestamp",
    "feature_timestamp",
    "created_timestamp",
    "updated_at",
}


@dataclass(frozen=True)
class DriftFeatureResult:
    feature_view: str
    feature: str
    drift_score: float
    passed: bool
    reference_rows: int
    current_rows: int
    threshold: float


def split_reference_current(
    frame: pd.DataFrame,
    timestamp_column: str = "event_timestamp",
    current_days: int = 7,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    if frame.empty or timestamp_column not in frame.columns:
        midpoint = len(frame) // 2
        return frame.iloc[:midpoint], frame.iloc[midpoint:]
    normalized = frame.copy()
    normalized[timestamp_column] = pd.to_datetime(normalized[timestamp_column], utc=True)
    cutoff = normalized[timestamp_column].max() - pd.Timedelta(days=current_days)
    reference = normalized[normalized[timestamp_column] <= cutoff]
    current = normalized[normalized[timestamp_column] > cutoff]
    if reference.empty or current.empty:
        midpoint = len(normalized) // 2
        reference = normalized.iloc[:midpoint]
        current = normalized.iloc[midpoint:]
    return reference, current


def numeric_feature_columns(frame: pd.DataFrame) -> list[str]:
    return [
        column
        for column in frame.select_dtypes(include="number").columns
        if column not in ID_COLUMNS and not column.endswith("_id")
    ]


def analyze_feature_view(
    frame: pd.DataFrame,
    feature_view: str,
    threshold: float,
    current_days: int,
    timestamp_column: str = "event_timestamp",
) -> list[DriftFeatureResult]:
    reference, current = split_reference_current(frame, timestamp_column, current_days)
    results: list[DriftFeatureResult] = []
    for feature in numeric_feature_columns(frame):
        score = calculate_psi(reference[feature], current[feature])
        results.append(
            DriftFeatureResult(
                feature_view=feature_view,
                feature=feature,
                drift_score=score,
                passed=score < threshold,
                reference_rows=int(len(reference)),
                current_rows=int(len(current)),
                threshold=threshold,
            )
        )
    return results


def monitoring_rows(run_id: str, results: list[DriftFeatureResult]) -> list[dict[str, Any]]:
    now = datetime.now(timezone.utc)
    return [
        {
            "run_id": run_id,
            "feature_name": f"{result.feature_view}.{result.feature}",
            "drift_score": result.drift_score,
            "passed": result.passed,
            "metrics": {
                "threshold": result.threshold,
                "reference_rows": result.reference_rows,
                "current_rows": result.current_rows,
                "feature_view": result.feature_view,
                "drift_kind": "feature_drift",
            },
            "created_timestamp": now,
        }
        for result in results
    ]


def pushgateway_samples(run_id: str, results: list[DriftFeatureResult]) -> list[MetricSample]:
    samples = [
        MetricSample("recsys_ml_feature_drift_run_timestamp_seconds", datetime.now(timezone.utc).timestamp(), {"run_id": run_id})
    ]
    for result in results:
        labels = {"feature_view": result.feature_view, "feature": result.feature}
        samples.extend(
            [
                MetricSample("recsys_ml_feature_drift_psi", result.drift_score, labels),
                MetricSample("recsys_ml_feature_drift_passed", 1.0 if result.passed else 0.0, labels),
                MetricSample(
                    "recsys_ml_feature_drift_reference_rows",
                    result.reference_rows,
                    {"feature_view": result.feature_view},
                ),
                MetricSample(
                    "recsys_ml_feature_drift_current_rows",
                    result.current_rows,
                    {"feature_view": result.feature_view},
                ),
            ]
        )
    return samples


def run_offline_feature_drift(
    run_id: str,
    offline_root: str,
    report_path: str,
    feature_views: list[str] | None = None,
    threshold: float = 0.15,
    current_days: int = 7,
    pushgateway_url: str | None = None,
) -> dict[str, Any]:
    results: list[DriftFeatureResult] = []
    errors: list[str] = []
    for feature_view in feature_views or DEFAULT_FEATURE_VIEWS:
        try:
            frame = read_feature_table(f"{offline_root.rstrip('/')}/{feature_view}")
            timestamp_column = "event_timestamp" if "event_timestamp" in frame.columns else "feature_timestamp"
            results.extend(analyze_feature_view(frame, feature_view, threshold, current_days, timestamp_column))
        except Exception as exc:
            errors.append(f"{feature_view}: {exc}")

    rows = monitoring_rows(run_id, results)
    if rows:
        with connect() as connection:
            upsert_rows(connection, MONITORING_FEATURE_DRIFT_RUNS, rows)

    push_metrics(
        pushgateway_samples(run_id, results),
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
    parser = argparse.ArgumentParser(description="Run offline feature drift monitoring and push metrics.")
    parser.add_argument("--run-id", default=os.getenv("FEATURE_DRIFT_RUN_ID", datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")))
    parser.add_argument("--offline-root", default=os.getenv("FEAST_OFFLINE_ROOT", "s3://recsys-feature-store/offline"))
    parser.add_argument(
        "--report-path",
        default=os.getenv("OFFLINE_FEATURE_DRIFT_REPORT_PATH", "s3://recsys-lake/monitoring/offline_feature_drift/report.json"),
    )
    parser.add_argument("--feature-view", action="append", dest="feature_views")
    parser.add_argument("--threshold", type=float, default=float(os.getenv("RETRAIN_PSI_THRESHOLD", "0.15")))
    parser.add_argument("--current-days", type=int, default=int(os.getenv("FEATURE_DRIFT_CURRENT_DAYS", "7")))
    parser.add_argument("--pushgateway-url", default=os.getenv("PUSHGATEWAY_URL", ""))
    parser.add_argument("--fail-on-drift", action="store_true")
    args = parser.parse_args()
    report = run_offline_feature_drift(
        args.run_id,
        args.offline_root,
        args.report_path,
        feature_views=args.feature_views,
        threshold=args.threshold,
        current_days=args.current_days,
        pushgateway_url=args.pushgateway_url or None,
    )
    print(json.dumps(report, indent=2, sort_keys=True, default=str))
    return 1 if args.fail_on_drift and not report["passed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
