from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from evaluate_bst import evaluate_bst


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate the best BST checkpoint produced by Ray Tune")
    parser.add_argument("--ray-result-path", default="/workspace/recsys/data_platform/output/ml/ray/best_result.json")
    parser.add_argument("--config-path", default="configs/local/bst.yaml")
    parser.add_argument("--split", default="test")
    parser.add_argument("--metrics-path", default="")
    parser.add_argument("--dataset-metadata-path", default="")
    args = parser.parse_args()

    payload = json.loads(Path(args.ray_result_path).read_text(encoding="utf-8"))
    config_path = payload.get("best_config_path") or args.config_path
    checkpoint_path = payload["checkpoint_path"]
    if payload.get("mlflow_run_id"):
        os.environ["MLFLOW_RUN_ID"] = payload["mlflow_run_id"]
    evaluate_bst(
        config_path=config_path,
        checkpoint_path=checkpoint_path,
        split=args.split,
        metrics_path=args.metrics_path or None,
        dataset_metadata_path=args.dataset_metadata_path or None,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
