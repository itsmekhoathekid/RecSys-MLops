from __future__ import annotations

import argparse
import json
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import yaml

from model_registry import register_model_config
from train import run_training


OBJECTIVE_METRIC = "val/ndcg@10"
RAY_OBJECTIVE_METRIC = "val_ndcg_at_10"


@contextmanager
def patched_env(values: dict[str, str]):
    original = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for key, value in original.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def load_config(path: str) -> dict[str, Any]:
    with Path(path).open("r", encoding="utf-8") as file:
        return yaml.safe_load(file)


def scan_split_cardinalities(split_dir: str) -> dict[str, int]:
    maxima = {
        "item_num": 0,
        "category_num": 0,
        "brand_num": 0,
        "price_bucket_num": 0,
        "time_bucket_num": 0,
        "event_type_num": 0,
    }
    mappings = {
        "item_num": ["target_item_id", "hist_item_id"],
        "category_num": ["target_category", "hist_category"],
        "brand_num": ["target_brand", "hist_brand"],
        "price_bucket_num": ["target_price_bucket", "hist_price_bucket"],
        "time_bucket_num": ["hist_time"],
        "event_type_num": ["hist_event_type"],
    }
    for split in ("train", "val", "test"):
        path = Path(split_dir) / f"{split}.jsonl"
        if not path.exists():
            continue
        with path.open("r", encoding="utf-8") as file:
            for line in file:
                if not line.strip():
                    continue
                row = json.loads(line)
                for output_key, source_keys in mappings.items():
                    for source_key in source_keys:
                        value = row.get(source_key)
                        if value is None:
                            continue
                        values = value if isinstance(value, list) else [value]
                        non_negative = [int(item) for item in values if int(item) >= 0]
                        if non_negative:
                            maxima[output_key] = max(maxima[output_key], max(non_negative) + 1)
    return maxima


def write_config(config: dict[str, Any], path: str | Path) -> str:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    return str(target)


def build_trial_config(
    base_config: dict[str, Any],
    trial_config: dict[str, Any],
    trial_dir: Path,
    split_dir: str,
    num_epochs: int,
) -> str:
    config = json.loads(json.dumps(base_config))
    config["training_args"]["learning_rate"] = float(trial_config["learning_rate"])
    config["training_args"]["weight_decay"] = float(trial_config["weight_decay"])
    config["training_args"]["num_epochs"] = int(num_epochs)
    config["training_args"]["num_workers"] = int(trial_config.get("num_workers", 0))
    config["model_args"]["hidden_dropout_prob"] = float(trial_config["hidden_dropout_prob"])
    config["model_args"]["attn_dropout_prob"] = float(
        trial_config.get("attn_dropout_prob", config["model_args"].get("attn_dropout_prob", 0.2))
    )
    for key, value in scan_split_cardinalities(split_dir).items():
        config["model_args"][key] = max(int(config["model_args"].get(key, 0)), value)
    config["model_args"]["padding_idx"] = 0
    config["model_args"]["save_path"] = str(trial_dir / "checkpoints")
    config["data_args"]["num_workers"] = int(trial_config.get("num_workers", 0))
    config["data_args"]["train_data_path"] = str(Path(split_dir) / "train.jsonl")
    config["data_args"]["val_data_path"] = str(Path(split_dir) / "val.jsonl")
    config["data_args"]["test_data_path"] = str(Path(split_dir) / "test.jsonl")
    config["data_args"]["padding_idx"] = 0
    return write_config(config, trial_dir / "bst_trial.yaml")


def metric_payload(training_result: dict[str, Any]) -> dict[str, float]:
    metrics = training_result.get("metrics", {})
    value = float(metrics.get(OBJECTIVE_METRIC, 0.0))
    return {
        RAY_OBJECTIVE_METRIC: value,
        "val_loss": float(metrics.get("val/loss", 0.0)),
        "train_loss": float(metrics.get("train/loss", 0.0)),
        "best_score": float(metrics.get("best_score", value)),
    }


def run_trial(
    trial_config: dict[str, Any],
    base_config_path: str,
    output_dir: str,
    split_dir: str,
    training_percent: float,
    num_epochs: int,
) -> dict[str, Any]:
    from ray import tune

    context = tune.get_context()
    trial_name = context.get_trial_name() or f"trial-{os.getpid()}"
    trial_dir = Path(output_dir) / "trials" / trial_name
    config_path = build_trial_config(
        base_config=load_config(base_config_path),
        trial_config=trial_config,
        trial_dir=trial_dir,
        split_dir=split_dir,
        num_epochs=num_epochs,
    )
    metrics_path = trial_dir / "training_result.json"
    with patched_env(
        {
            "SKIP_MODEL_REGISTRY": "1",
            "MLFLOW_RUN_NAME": f"ray-{trial_name}",
        }
    ):
        result = run_training(
            config_path=config_path,
            training_percent=training_percent,
            num_epochs=num_epochs,
            metrics_path=str(metrics_path),
        )

    report = {
        **metric_payload(result),
        "checkpoint_path": result["checkpoint_path"],
        "artifact_uri": result.get("artifact_uri") or result["checkpoint_path"],
        "config_path": config_path,
        "metrics_path": str(metrics_path),
    }
    tune.report(report)
    return report


def register_best_result(best_payload: dict[str, Any]) -> None:
    postgres_uri = os.getenv("MODEL_REGISTRY_POSTGRES_URI") or os.getenv("POSTGRES_MODEL_REGISTRY_URI")
    if not postgres_uri:
        return
    metrics = best_payload.get("metrics", {})
    config = load_config(best_payload["best_config_path"])
    register_model_config(
        postgres_uri=postgres_uri,
        model_name=config["model_args"].get("model_name", "BST"),
        model_version=os.getenv("MODEL_VERSION", best_payload["best_trial_name"]),
        artifact_uri=best_payload.get("artifact_uri") or best_payload["checkpoint_path"],
        mlflow_run_id=best_payload.get("mlflow_run_id"),
        metrics=metrics,
        config=config,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Tune and train BST with Ray Tune")
    parser.add_argument("--base-config-path", default="/opt/recsys/configs/local/bst.yaml")
    parser.add_argument("--split-dir", default="/workspace/recsys/notebooks/data/bst_split")
    parser.add_argument("--output-dir", default="/workspace/recsys/data_platform/output/ml/ray")
    parser.add_argument("--training-percent", type=float, default=0.01)
    parser.add_argument("--num-epochs", type=int, default=1)
    parser.add_argument("--max-trials", type=int, default=2)
    parser.add_argument("--parallel-trials", type=int, default=1)
    parser.add_argument("--cpus-per-trial", type=float, default=1.0)
    parser.add_argument("--gpus-per-trial", type=float, default=0.0)
    parser.add_argument("--ray-address", default="auto")
    parser.add_argument("--best-result-path", default="")
    args = parser.parse_args()

    import ray
    from ray import tune

    ray.init(address=args.ray_address, ignore_reinit_error=True)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    search_space = {
        "learning_rate": tune.choice([1e-3, 5e-4]),
        "weight_decay": tune.choice([1e-5, 1e-4]),
        "hidden_dropout_prob": tune.choice([0.1, 0.2]),
        "num_workers": 0,
    }
    trainable = tune.with_resources(
        tune.with_parameters(
            run_trial,
            base_config_path=args.base_config_path,
            output_dir=str(output_dir),
            split_dir=args.split_dir,
            training_percent=args.training_percent,
            num_epochs=args.num_epochs,
        ),
        resources={"cpu": args.cpus_per_trial, "gpu": args.gpus_per_trial},
    )
    tuner = tune.Tuner(
        trainable,
        tune_config=tune.TuneConfig(
            metric=RAY_OBJECTIVE_METRIC,
            mode="max",
            num_samples=max(1, args.max_trials),
            max_concurrent_trials=args.parallel_trials,
        ),
        param_space=search_space,
    )
    result_grid = tuner.fit()
    best = result_grid.get_best_result(metric=RAY_OBJECTIVE_METRIC, mode="max")
    best_metrics_path = Path(best.metrics["metrics_path"])
    training_result = json.loads(best_metrics_path.read_text(encoding="utf-8"))
    best_payload = {
        "best_trial_name": best.path.split("/")[-1],
        "best_config": best.config,
        "best_config_path": best.metrics["config_path"],
        "checkpoint_path": best.metrics["checkpoint_path"],
        "artifact_uri": best.metrics["artifact_uri"],
        "metrics": training_result.get("metrics", {}),
        "ray_metrics": {
            key: value
            for key, value in best.metrics.items()
            if isinstance(value, (int, float, str))
        },
    }
    best_result_path = Path(args.best_result_path or output_dir / "best_result.json")
    best_result_path.parent.mkdir(parents=True, exist_ok=True)
    best_result_path.write_text(
        json.dumps(best_payload, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    register_best_result(best_payload)
    print(json.dumps(best_payload, indent=2, sort_keys=True))
    ray.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
