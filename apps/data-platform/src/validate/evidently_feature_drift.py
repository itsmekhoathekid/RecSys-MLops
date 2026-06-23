from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from validate.great_expectations_runner import write_report
from warehouse.connection import connect
from warehouse.schemas import MONITORING_FEATURE_DRIFT_RUNS
from warehouse.writer import upsert_rows


FEATURE_COLUMNS = [
    "event_type_id",
    "category_id",
    "brand_id",
    "price_bucket",
    "price",
]


def calculate_psi(expected: pd.Series, actual: pd.Series, buckets: int = 10) -> float:
    expected_values = pd.to_numeric(expected, errors="coerce").dropna().to_numpy(dtype=float)
    actual_values = pd.to_numeric(actual, errors="coerce").dropna().to_numpy(dtype=float)
    if expected_values.size == 0 or actual_values.size == 0:
        return 0.0
    quantiles = np.unique(np.quantile(expected_values, np.linspace(0, 1, buckets + 1)))
    if quantiles.size < 3:
        minimum = min(float(expected_values.min()), float(actual_values.min()))
        maximum = max(float(expected_values.max()), float(actual_values.max()))
        if minimum == maximum:
            return 0.0
        quantiles = np.asarray([minimum, (minimum + maximum) / 2.0, maximum])
    expected_counts, _ = np.histogram(expected_values, bins=quantiles)
    actual_counts, _ = np.histogram(actual_values, bins=quantiles)
    epsilon = 1e-4
    expected_pct = np.maximum(expected_counts / max(expected_counts.sum(), 1), epsilon)
    actual_pct = np.maximum(actual_counts / max(actual_counts.sum(), 1), epsilon)
    return float(np.sum((actual_pct - expected_pct) * np.log(actual_pct / expected_pct)))


def read_reference_and_current(connection: Any, timestamp_column: str, current_days: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    frame = pd.read_sql_query("SELECT * FROM production.fact_behavior_events", connection)
    if frame.empty or timestamp_column not in frame.columns:
        return frame, frame
    frame[timestamp_column] = pd.to_datetime(frame[timestamp_column], utc=True)
    cutoff = frame[timestamp_column].max() - pd.Timedelta(days=current_days)
    reference = frame[frame[timestamp_column] <= cutoff]
    current = frame[frame[timestamp_column] > cutoff]
    if reference.empty:
        midpoint = len(frame) // 2
        reference = frame.iloc[:midpoint]
        current = frame.iloc[midpoint:]
    return reference, current


def run_feature_drift(
    run_id: str,
    report_path: str,
    threshold: float = 0.15,
    current_days: int = 7,
) -> dict[str, Any]:
    with connect() as connection:
        reference, current = read_reference_and_current(connection, "event_timestamp", current_days)
        rows = []
        for feature_name in FEATURE_COLUMNS:
            if feature_name not in reference.columns or feature_name not in current.columns:
                continue
            score = calculate_psi(reference[feature_name], current[feature_name])
            rows.append(
                {
                    "run_id": run_id,
                    "feature_name": feature_name,
                    "drift_score": score,
                    "passed": score < threshold,
                    "metrics": {
                        "threshold": threshold,
                        "reference_rows": int(len(reference)),
                        "current_rows": int(len(current)),
                    },
                    "created_timestamp": datetime.now(timezone.utc),
                }
            )
        upsert_rows(connection, MONITORING_FEATURE_DRIFT_RUNS, rows)

    report = {
        "run_id": run_id,
        "passed": all(row["passed"] for row in rows),
        "features": rows,
    }
    report["report_path"] = write_report(report_path, report)
    if not report["passed"]:
        raise SystemExit(json.dumps(report, indent=2, sort_keys=True, default=str))
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Evidently-style feature drift monitoring.")
    parser.add_argument("--run-id", default=os.getenv("FEATURE_DRIFT_RUN_ID", datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")))
    parser.add_argument(
        "--report-path",
        default=os.getenv("EVIDENTLY_REPORT_PATH", "s3://recsys-lake/monitoring/evidently/feature_drift.json"),
    )
    parser.add_argument("--threshold", type=float, default=float(os.getenv("EVIDENTLY_PSI_THRESHOLD", "0.15")))
    parser.add_argument("--current-days", type=int, default=int(os.getenv("EVIDENTLY_CURRENT_DAYS", "7")))
    args = parser.parse_args()
    print(
        json.dumps(
            run_feature_drift(args.run_id, args.report_path, threshold=args.threshold, current_days=args.current_days),
            indent=2,
            sort_keys=True,
            default=str,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

