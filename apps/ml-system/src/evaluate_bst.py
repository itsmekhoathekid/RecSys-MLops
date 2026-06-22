from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import torch

from models import Trainer, load_config, recommenderDataset


def _log_eval_to_mlflow(metrics: dict, checkpoint_path: str) -> None:
    tracking_uri = os.getenv("MLFLOW_TRACKING_URI")
    run_id = os.getenv("MLFLOW_RUN_ID")
    if not tracking_uri or not run_id:
        return

    import mlflow

    mlflow.set_tracking_uri(tracking_uri)
    with mlflow.start_run(run_id=run_id):
        for name, value in metrics.items():
            if isinstance(value, (int, float)):
                mlflow.log_metric(f"test_{name}".replace("@", "_at_"), float(value))
        mlflow.log_dict(
            {"checkpoint_path": checkpoint_path, "metrics": metrics},
            "metrics/test_metrics.json",
        )


def evaluate_bst(
    config_path: str,
    checkpoint_path: str,
    split: str = "test",
    metrics_path: str | None = None,
) -> dict:
    config = load_config(config_path)
    trainer = Trainer(config)
    checkpoint = torch.load(checkpoint_path, map_location=trainer.device)
    trainer.model.load_state_dict(checkpoint["model_state_dict"])

    dataset = recommenderDataset(config["data_args"], split=split, percent=1.0)
    loader = trainer.get_data_loader(dataset, shuffle=False)
    metrics = trainer.evaluate(loader)
    result = {
        "checkpoint_path": checkpoint_path,
        "split": split,
        "metrics": metrics,
    }
    if metrics_path:
        Path(metrics_path).parent.mkdir(parents=True, exist_ok=True)
        Path(metrics_path).write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    _log_eval_to_mlflow(metrics, checkpoint_path)
    print(json.dumps(result, indent=2, sort_keys=True))
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate a trained BST checkpoint")
    parser.add_argument("--config-path", default="./configs/local/bst.yaml")
    parser.add_argument("--checkpoint-path", default="./notebooks/data/BST")
    parser.add_argument("--split", default="test")
    parser.add_argument("--metrics-path", default="")
    args = parser.parse_args()

    evaluate_bst(
        config_path=args.config_path,
        checkpoint_path=args.checkpoint_path,
        split=args.split,
        metrics_path=args.metrics_path or None,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

