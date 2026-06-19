from __future__ import annotations

import argparse
import json
from pathlib import Path

from pipelines.model_pipeline.evaluate_bst import evaluate_bst


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate the best BST checkpoint produced by Ray Tune")
    parser.add_argument("--ray-result-path", default="/workspace/recsys/data_pipeline/output/ml/ray/best_result.json")
    parser.add_argument("--config-path", default="config/bst.yaml")
    parser.add_argument("--split", default="test")
    parser.add_argument("--metrics-path", default="")
    args = parser.parse_args()

    payload = json.loads(Path(args.ray_result_path).read_text(encoding="utf-8"))
    config_path = payload.get("best_config_path") or args.config_path
    checkpoint_path = payload["checkpoint_path"]
    evaluate_bst(
        config_path=config_path,
        checkpoint_path=checkpoint_path,
        split=args.split,
        metrics_path=args.metrics_path or None,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

