import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from lineage.mlflow_dataset_lineage import dataset_versions, load_dataset_metadata, log_dataset_lineage
from models import *
from registry.model_registry import register_model_config


def _flatten(prefix, value):
    if isinstance(value, dict):
        out = {}
        for key, child in value.items():
            out.update(_flatten(f"{prefix}.{key}" if prefix else key, child))
        return out
    return {prefix: value}


def _mlflow_metric_name(name):
    return name.replace("@", "_at_")


def _log_to_mlflow(config, metrics, checkpoint_path, dataset_metadata=None):
    tracking_uri = os.getenv("MLFLOW_TRACKING_URI")
    if not tracking_uri:
        return None, None

    import mlflow

    mlflow.set_tracking_uri(tracking_uri)
    mlflow.set_experiment(os.getenv("MLFLOW_EXPERIMENT_NAME", "recsys-bst-ranking"))
    with mlflow.start_run(run_name=os.getenv("MLFLOW_RUN_NAME", "bst-training")) as run:
        log_dataset_lineage(
            mlflow,
            dataset_metadata,
            {"train": "training", "val": "validation"},
        )
        for name, value in _flatten("", config).items():
            if isinstance(value, (str, int, float, bool)) or value is None:
                mlflow.log_param(name, value)
        for name, value in metrics.items():
            if isinstance(value, (int, float)):
                mlflow.log_metric(_mlflow_metric_name(name), float(value))
        if checkpoint_path and Path(checkpoint_path).exists():
            mlflow.log_artifact(checkpoint_path, artifact_path="model")
        mlflow.log_dict(config, "configs/local/bst.yaml")
        mlflow.log_dict(metrics, "metrics/training_metrics.json")
        artifact_uri = mlflow.get_artifact_uri("model")
        return run.info.run_id, artifact_uri


def _maybe_register_config(config, metrics, checkpoint_path, run_id, artifact_uri):
    if os.getenv("SKIP_MODEL_REGISTRY", "").lower() in {"1", "true", "yes"}:
        return
    postgres_uri = os.getenv("MODEL_REGISTRY_POSTGRES_URI") or os.getenv("POSTGRES_MODEL_REGISTRY_URI")
    if not postgres_uri:
        return
    model_name = config["model_args"].get("model_name", "BST")
    model_version = os.getenv("MODEL_VERSION") or datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    register_model_config(
        postgres_uri=postgres_uri,
        model_name=model_name,
        model_version=model_version,
        artifact_uri=artifact_uri or checkpoint_path or "",
        mlflow_run_id=run_id,
        metrics=metrics,
        config=config,
    )


def run_training(
    config_path="./configs/local/bst.yaml",
    training_percent=None,
    num_epochs=None,
    metrics_path=None,
    dataset_metadata_path=None,
):
    config = load_config(config_path)
    metadata_path = dataset_metadata_path or config.get("data_args", {}).get("dataset_metadata_path") or os.getenv(
        "DATASET_VERSION_METADATA_PATH"
    )
    dataset_metadata = load_dataset_metadata(metadata_path)
    if num_epochs is not None:
        config["training_args"]["num_epochs"] = num_epochs

    trainer = Trainer(config)
    percent = training_percent if training_percent is not None else config["data_args"].get("percent", 1.0)
    training_data = recommenderDataset(config["data_args"], split="train", percent=percent)
    val_data = recommenderDataset(config["data_args"], split="val", percent=percent)

    train_loader = trainer.get_data_loader(training_data, shuffle=config["data_args"]["shuffle"])
    val_loader = trainer.get_data_loader(val_data, shuffle=False)
    best_checkpoint_path = None
    final_metrics = {}

    for epoch in range(config["training_args"]["num_epochs"]):
        train_metrics = trainer.train(train_loader)
        val_metrics = trainer.evaluate(val_loader)

        for metric_name, metric_value in train_metrics.items():
            trainer.logger.log(f"train/{metric_name}", metric_value, epoch)

        for metric_name, metric_value in val_metrics.items():
            trainer.logger.log(f"val/{metric_name}", metric_value, epoch)

        ndcg_10_val = val_metrics.get("ndcg@10", 0)
        best_score = trainer.get_best_score()

        if ndcg_10_val > best_score:
            best_checkpoint_path = trainer.save_model(epoch, ndcg_10_val)
            trainer.best_score = ndcg_10_val
            print(f"New best model saved with NDCG@10: {ndcg_10_val:.4f}")
        else:
            print(f"No improvement in NDCG@10: {ndcg_10_val:.4f} (best: {best_score:.4f})")

        final_metrics = {
            "epoch": epoch,
            "best_score": trainer.get_best_score(),
            **{f"train/{key}": value for key, value in train_metrics.items()},
            **{f"val/{key}": value for key, value in val_metrics.items()},
        }

    if best_checkpoint_path is None:
        best_checkpoint_path = trainer.save_model(
            config["training_args"]["num_epochs"] - 1,
            trainer.get_best_score(),
        )

    run_id, artifact_uri = _log_to_mlflow(config, final_metrics, best_checkpoint_path, dataset_metadata=dataset_metadata)
    _maybe_register_config(config, final_metrics, best_checkpoint_path, run_id, artifact_uri)

    result = {
        "checkpoint_path": best_checkpoint_path,
        "mlflow_run_id": run_id,
        "artifact_uri": artifact_uri or best_checkpoint_path,
        "metrics": final_metrics,
        "dataset_versions": dataset_versions(dataset_metadata),
    }
    if metrics_path:
        Path(metrics_path).parent.mkdir(parents=True, exist_ok=True)
        Path(metrics_path).write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(result, indent=2, sort_keys=True))
    return result


def train():
    args = argparse.ArgumentParser()
    args.add_argument("--config_path", type=str, default="./configs/local/bst.yaml")
    args.add_argument("--training-percent", type=float, default=None)
    args.add_argument("--num-epochs", type=int, default=None)
    args.add_argument("--metrics-path", default="")
    args.add_argument("--dataset-metadata-path", default="")
    parsed = args.parse_args()
    run_training(
        config_path=parsed.config_path,
        training_percent=parsed.training_percent,
        num_epochs=parsed.num_epochs,
        metrics_path=parsed.metrics_path or None,
        dataset_metadata_path=parsed.dataset_metadata_path or None,
    )


if __name__ == "__main__":
    train()
