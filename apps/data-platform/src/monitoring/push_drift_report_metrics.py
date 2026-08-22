from __future__ import annotations

import argparse
import json
import os
import time
from typing import Any

from mlops.trigger_kubeflow_retrain import read_json
from monitoring.pushgateway import MetricSample, push_metrics


def drift_report_samples(
    report: dict[str, Any], *, timestamp_seconds: float | None = None
) -> tuple[str, list[MetricSample]]:
    run_id = str(report.get("run_id") or "unknown")
    timestamp = time.time() if timestamp_seconds is None else timestamp_seconds
    return run_id, [
        MetricSample(
            "recsys_ml_feature_drift_report_available",
            1.0,
            {
                "run_id": run_id,
                "passed": str(report.get("passed", False)).lower(),
            },
        ),
        MetricSample(
            "recsys_ml_feature_drift_report_timestamp_seconds",
            float(int(timestamp)),
            {"run_id": run_id},
        ),
    ]


def publish_drift_report_metrics(
    report_path: str, *, gateway_url: str | None = None
) -> dict[str, Any]:
    if not report_path:
        raise ValueError("A drift report path is required")
    report = read_json(report_path)
    run_id, samples = drift_report_samples(report)
    pushed = push_metrics(
        samples,
        job="recsys_offline_feature_drift_report",
        gateway_url=gateway_url,
        grouping_key={"run_id": run_id},
    )
    return {"pushed_drift_report_metrics": pushed, "run_id": run_id}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Publish availability metrics for an offline feature drift report."
    )
    parser.add_argument(
        "--drift-report-path",
        default=os.getenv("OFFLINE_FEATURE_DRIFT_REPORT_PATH", ""),
    )
    parser.add_argument(
        "--pushgateway-url",
        default=os.getenv("PUSHGATEWAY_URL", ""),
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    result = publish_drift_report_metrics(
        args.drift_report_path,
        gateway_url=args.pushgateway_url or None,
    )
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
