from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from mlops.trigger_kubeflow_retrain import parse_pipeline_args, trigger_retrain
from validate.offline_feature_drift import run_offline_feature_drift


def _write_table(path: Path, frame: pd.DataFrame) -> None:
    path.mkdir(parents=True, exist_ok=True)
    frame.to_parquet(path / "part-00000.parquet", index=False)


def generate_smoke_features(workdir: Path) -> tuple[Path, Path]:
    baseline_root = workdir / "baseline"
    current_root = workdir / "current"
    rows = 120
    baseline = pd.DataFrame(
        {
            "item_id": list(range(rows)),
            "views_1h": [1 + (index % 5) for index in range(rows)],
            "carts_1h": [index % 3 for index in range(rows)],
            "popularity_score": [0.05 + (index % 7) * 0.01 for index in range(rows)],
        }
    )
    current = pd.DataFrame(
        {
            "item_id": list(range(rows)),
            "views_1h": [80 + (index % 13) for index in range(rows)],
            "carts_1h": [20 + (index % 5) for index in range(rows)],
            "popularity_score": [0.75 + (index % 9) * 0.01 for index in range(rows)],
        }
    )
    _write_table(baseline_root / "item_features", baseline)
    _write_table(current_root / "item_features", current)
    return baseline_root, current_root


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate smoke feature data, run drift, and trigger KFP retraining.")
    parser.add_argument("--workdir", default="/tmp/recsys-retrain-smoke")
    parser.add_argument("--run-id", default=f"retrain-smoke-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}")
    parser.add_argument("--kfp-endpoint", default="http://127.0.0.1:8888")
    parser.add_argument("--experiment-name", default="recsys-observability-retrain")
    parser.add_argument("--pipeline-package-path", default="infra/kubeflow/compiled/bst_training_pipeline.yaml")
    parser.add_argument("--pushgateway-url", default="")
    parser.add_argument("--pipeline-arg", action="append", default=[])
    parser.add_argument("--skip-trigger", action="store_true")
    args = parser.parse_args()

    workdir = Path(args.workdir)
    baseline_root, current_root = generate_smoke_features(workdir)
    report_path = workdir / "offline_feature_drift_report.json"
    report = run_offline_feature_drift(
        args.run_id,
        str(report_path),
        feature_tables=["item_features"],
        threshold=0.15,
        pushgateway_url=args.pushgateway_url or None,
        current_feature_root=str(current_root),
        baseline_path=str(baseline_root),
        sample_rows=1000,
        bootstrap_baseline=False,
    )
    payload: dict[str, object] = {"drift_report": report}
    if not args.skip_trigger:
        pipeline_args = parse_pipeline_args(args.pipeline_arg)
        pipeline_args.setdefault("source_run_path", f"smoke://{args.run_id}")
        result = trigger_retrain(
            str(report_path),
            args.kfp_endpoint,
            args.experiment_name,
            args.pipeline_package_path,
            pushgateway_url=args.pushgateway_url or None,
            pipeline_arguments=pipeline_args,
        )
        payload["retrain_trigger"] = asdict(result)
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    return 0 if report["passed"] is False else 1


if __name__ == "__main__":
    raise SystemExit(main())
