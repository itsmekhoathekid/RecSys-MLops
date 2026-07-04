from __future__ import annotations

import argparse
import json
import os
import shutil
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import torch
import yaml
from torch.utils.data import DataLoader
from torch.utils.data.distributed import DistributedSampler

from lineage.mlflow_dataset_lineage import dataset_versions, load_dataset_metadata
from models import BST, recommenderDataset
from training.ray_tune_train_bst import scan_split_cardinalities
from training.train import _log_to_mlflow, _maybe_register_config


OBJECTIVE_METRIC = "val/ndcg@10"


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


def load_config(path: str | Path) -> dict[str, Any]:
    with Path(path).open("r", encoding="utf-8") as file:
        return yaml.safe_load(file)


def write_config(config: dict[str, Any], path: str | Path) -> str:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    return str(target)


def load_tune_result(path: str | Path) -> dict[str, Any]:
    target = Path(path)
    if not target.exists():
        return {}
    return json.loads(target.read_text(encoding="utf-8"))


def apply_tuned_hyperparameters(config: dict[str, Any], tune_result: dict[str, Any]) -> None:
    best_config = tune_result.get("best_config") or {}
    if "learning_rate" in best_config:
        config["training_args"]["learning_rate"] = float(best_config["learning_rate"])
    if "weight_decay" in best_config:
        config["training_args"]["weight_decay"] = float(best_config["weight_decay"])
    if "hidden_dropout_prob" in best_config:
        config["model_args"]["hidden_dropout_prob"] = float(best_config["hidden_dropout_prob"])
    if "attn_dropout_prob" in best_config:
        config["model_args"]["attn_dropout_prob"] = float(best_config["attn_dropout_prob"])


def build_distributed_config(
    base_config_path: str,
    tune_result_path: str,
    split_dir: str,
    output_dir: str,
    num_epochs: int,
    num_workers: int,
    dataset_metadata_path: str | None,
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    tune_result = load_tune_result(tune_result_path) if tune_result_path else {}
    tuned_config_path = tune_result.get("best_config_path")
    source_config_path = tuned_config_path if tuned_config_path and Path(tuned_config_path).exists() else base_config_path
    config = load_config(source_config_path)
    apply_tuned_hyperparameters(config, tune_result)

    config["training_args"]["num_epochs"] = int(num_epochs)
    config["training_args"]["num_workers"] = int(num_workers)
    config["data_args"]["num_workers"] = int(num_workers)
    config["data_args"]["train_data_path"] = str(Path(split_dir) / "train.jsonl")
    config["data_args"]["val_data_path"] = str(Path(split_dir) / "val.jsonl")
    config["data_args"]["test_data_path"] = str(Path(split_dir) / "test.jsonl")
    config["model_args"]["padding_idx"] = 0
    config["data_args"]["padding_idx"] = 0
    config["model_args"]["save_path"] = str(Path(output_dir) / "distributed_checkpoints")
    if dataset_metadata_path:
        config["data_args"]["dataset_metadata_path"] = dataset_metadata_path

    for key, value in scan_split_cardinalities(split_dir).items():
        config["model_args"][key] = max(int(config["model_args"].get(key, 0)), value)

    config_path = write_config(config, Path(output_dir) / "distributed_train_config.yaml")
    return config_path, config, tune_result


def move_batch_to_device(batch: dict[str, Any], device: torch.device) -> dict[str, Any]:
    return {key: value.to(device) if torch.is_tensor(value) else value for key, value in batch.items()}


def forward_model(model: torch.nn.Module, batch: dict[str, torch.Tensor]) -> torch.Tensor:
    return model(
        hist_item_id=batch["hist_item_id"],
        hist_event_type=batch["hist_event_type"],
        hist_category=batch["hist_category"],
        hist_brand=batch["hist_brand"],
        hist_price_bucket=batch["hist_price_bucket"],
        hist_time=batch["hist_time"],
        target_item_id=batch["target_item_id"],
        target_category=batch["target_category"],
        target_brand=batch["target_brand"],
        target_price_bucket=batch["target_price_bucket"],
    )


def reduce_loss(total_loss: float, num_batches: int, device: torch.device) -> float:
    import torch.distributed as dist

    payload = torch.tensor([total_loss, float(num_batches)], dtype=torch.float64, device=device)
    if dist.is_available() and dist.is_initialized():
        dist.all_reduce(payload, op=dist.ReduceOp.SUM)
    return float(payload[0].item() / max(payload[1].item(), 1.0))


def broadcast_metrics(metrics: dict[str, Any] | None) -> dict[str, Any]:
    import torch.distributed as dist

    payload = [metrics]
    if dist.is_available() and dist.is_initialized():
        dist.broadcast_object_list(payload, src=0)
    return payload[0] or {}


def evaluate_on_rank_zero(
    model: torch.nn.Module,
    config: dict[str, Any],
    val_dataset: recommenderDataset,
    device: torch.device,
) -> dict[str, float]:
    from models.trainer import Trainer

    raw_model = model.module if hasattr(model, "module") else model
    raw_model.eval()
    loss_fn = torch.nn.BCEWithLogitsLoss()
    loader = DataLoader(
        val_dataset,
        batch_size=config["training_args"]["batch_size"],
        shuffle=False,
        num_workers=0,
        collate_fn=val_dataset.collate_fn,
    )
    all_probs: list[float] = []
    all_labels: list[float] = []
    all_group_keys: list[tuple[int, int]] = []
    total_loss = 0.0
    with torch.no_grad():
        for batch in loader:
            batch = move_batch_to_device(batch, device)
            logits = forward_model(raw_model, batch)
            labels = batch["label"].float()
            total_loss += float(loss_fn(logits, labels).item())
            all_probs.extend(torch.sigmoid(logits).detach().cpu().tolist())
            all_labels.extend(labels.detach().cpu().tolist())
            all_group_keys.extend(
                zip(
                    batch["user_id"].detach().cpu().tolist(),
                    batch["event_time"].detach().cpu().tolist(),
                )
            )
    metric_helper = Trainer.__new__(Trainer)
    metric_helper.ranking_ks = config["training_args"].get("ranking_ks", [1, 3, 5, 10])
    metrics = metric_helper._compute_metrics(all_labels, all_probs, all_group_keys)
    metrics["loss"] = total_loss / max(len(loader), 1)
    return {f"val/{key}": float(value) for key, value in metrics.items() if isinstance(value, (int, float))}


def save_checkpoint(
    model: torch.nn.Module,
    optimizer: torch.optim.Optimizer,
    scheduler: torch.optim.lr_scheduler.ReduceLROnPlateau,
    config: dict[str, Any],
    epoch: int,
    best_score: float,
) -> str:
    raw_model = model.module if hasattr(model, "module") else model
    base_path = Path(config["model_args"].get("save_path", "./data_platform/output/ml/checkpoints"))
    base_path.mkdir(parents=True, exist_ok=True)
    checkpoint_path = base_path / getattr(raw_model, "model_name", config["model_args"].get("model_name", "BST"))
    torch.save(
        {
            "epoch": epoch,
            "model_state_dict": raw_model.state_dict(),
            "optimizer_state_dict": optimizer.state_dict(),
            "scheduler_state_dict": scheduler.state_dict(),
            "score": best_score,
            "config": config,
            "distributed": {
                "backend": "torch.distributed",
                "strategy": "DistributedDataParallel",
            },
        },
        checkpoint_path,
    )
    return str(checkpoint_path)


def train_loop_per_worker(config: dict[str, Any]) -> None:
    from ray import train
    from ray.train import Checkpoint
    from ray.train.torch import prepare_data_loader, prepare_model

    context = train.get_context()
    rank = context.get_world_rank()
    world_size = context.get_world_size()
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

    model = BST(config["model_args"]).to(device)
    model = prepare_model(model)
    optimizer = torch.optim.Adam(
        model.parameters(),
        lr=float(config["training_args"]["learning_rate"]),
        weight_decay=float(config["training_args"]["weight_decay"]),
    )
    scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(optimizer, mode="min", factor=0.3, patience=2)
    loss_fn = torch.nn.BCEWithLogitsLoss()

    train_dataset = recommenderDataset(config["data_args"], split="train", percent=float(config["training_percent"]))
    val_dataset = recommenderDataset(config["data_args"], split="val", percent=float(config["training_percent"]))
    sampler = DistributedSampler(train_dataset, num_replicas=world_size, rank=rank, shuffle=True, drop_last=False)
    train_loader = DataLoader(
        train_dataset,
        batch_size=int(config["training_args"]["batch_size"]),
        sampler=sampler,
        shuffle=False,
        num_workers=0,
        collate_fn=train_dataset.collate_fn,
    )
    train_loader = prepare_data_loader(train_loader, add_dist_sampler=False)

    best_score = 0.0
    best_checkpoint_path = ""
    final_metrics: dict[str, Any] = {}
    for epoch in range(int(config["training_args"]["num_epochs"])):
        sampler.set_epoch(epoch)
        model.train()
        total_loss = 0.0
        num_batches = 0
        for batch in train_loader:
            batch = move_batch_to_device(batch, device)
            optimizer.zero_grad()
            logits = forward_model(model, batch)
            labels = batch["label"].float()
            loss = loss_fn(logits, labels)
            loss.backward()
            optimizer.step()
            total_loss += float(loss.item())
            num_batches += 1

        train_loss = reduce_loss(total_loss, num_batches, device)
        rank_zero_metrics = None
        checkpoint = None
        if rank == 0:
            val_metrics = evaluate_on_rank_zero(model, config, val_dataset, device)
            scheduler.step(val_metrics.get("val/loss", train_loss))
            score = float(val_metrics.get(OBJECTIVE_METRIC, 0.0))
            if score >= best_score:
                best_score = score
                best_checkpoint_path = save_checkpoint(model, optimizer, scheduler, config, epoch, best_score)
                checkpoint_dir = Path(config["output_dir"]) / "ray_train_checkpoints" / f"epoch_{epoch}"
                checkpoint_dir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(best_checkpoint_path, checkpoint_dir / Path(best_checkpoint_path).name)
                checkpoint = Checkpoint.from_directory(str(checkpoint_dir))
            rank_zero_metrics = {
                "epoch": epoch,
                "best_score": best_score,
                "train/loss": train_loss,
                **val_metrics,
            }

        final_metrics = broadcast_metrics(rank_zero_metrics)
        report_metrics = {
            key.replace("/", "_").replace("@", "_at_"): value
            for key, value in final_metrics.items()
            if isinstance(value, (int, float))
        }
        report_metrics.update(
            {
                "world_size": world_size,
                "rank": rank,
                "ddp_gradient_sync": True,
                "distributed_sampler": True,
            }
        )
        train.report(report_metrics, checkpoint=checkpoint)

    if rank == 0:
        metadata = load_dataset_metadata(config.get("dataset_metadata_path"))
        with patched_env({"MLFLOW_RUN_NAME": "ray-ddp-distributed-train"}):
            run_id, artifact_uri = _log_to_mlflow(
                config["logged_config"],
                final_metrics,
                best_checkpoint_path,
                dataset_metadata=metadata,
            )
        _maybe_register_config(config["logged_config"], final_metrics, best_checkpoint_path, run_id, artifact_uri)
        result = {
            "checkpoint_path": best_checkpoint_path,
            "artifact_uri": artifact_uri or best_checkpoint_path,
            "mlflow_run_id": run_id,
            "metrics": final_metrics,
            "dataset_versions": dataset_versions(metadata),
            "source": "kubeflow-ray-ddp-train",
            "best_config": config.get("tune_result", {}).get("best_config", {}),
            "best_config_path": config["config_path"],
            "tune_result_path": config.get("tune_result_path", ""),
            "distributed_training": {
                "strategy": "DistributedDataParallel",
                "world_size": world_size,
                "uses_distributed_sampler": True,
                "gradient_sync": True,
            },
        }
        best_result_path = Path(config["best_result_path"])
        best_result_path.parent.mkdir(parents=True, exist_ok=True)
        best_result_path.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
        print(json.dumps(result, indent=2, sort_keys=True))


def main() -> int:
    parser = argparse.ArgumentParser(description="Run final BST DDP training with Ray Train after Ray Tune")
    parser.add_argument("--base-config-path", default="/opt/recsys/configs/local/bst.yaml")
    parser.add_argument("--split-dir", default="/workspace/recsys/data_platform/output/ml/bst_split")
    parser.add_argument("--output-dir", default="/workspace/recsys/data_platform/output/ml/ray")
    parser.add_argument("--tune-result-path", default="/workspace/recsys/data_platform/output/ml/ray/tune_result.json")
    parser.add_argument("--best-result-path", default="/workspace/recsys/data_platform/output/ml/ray/best_result.json")
    parser.add_argument("--dataset-metadata-path", default="")
    parser.add_argument("--training-percent", type=float, default=0.02)
    parser.add_argument("--num-epochs", type=int, default=1)
    parser.add_argument("--num-workers", type=int, default=2)
    parser.add_argument("--cpus-per-worker", type=float, default=1.0)
    parser.add_argument("--gpus-per-worker", type=float, default=0.0)
    parser.add_argument("--ray-address", default="auto")
    args = parser.parse_args()

    import ray
    from ray.train import RunConfig, ScalingConfig
    from ray.train.torch import TorchTrainer

    ray.init(address=args.ray_address, ignore_reinit_error=True)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    config_path, distributed_config, tune_result = build_distributed_config(
        base_config_path=args.base_config_path,
        tune_result_path=args.tune_result_path,
        split_dir=args.split_dir,
        output_dir=args.output_dir,
        num_epochs=args.num_epochs,
        num_workers=0,
        dataset_metadata_path=args.dataset_metadata_path or None,
    )
    train_loop_config = {
        **distributed_config,
        "logged_config": distributed_config,
        "config_path": config_path,
        "output_dir": args.output_dir,
        "best_result_path": args.best_result_path,
        "tune_result_path": args.tune_result_path,
        "tune_result": tune_result,
        "training_percent": args.training_percent,
        "dataset_metadata_path": args.dataset_metadata_path or None,
    }
    resources_per_worker: dict[str, float] = {"CPU": args.cpus_per_worker}
    if args.gpus_per_worker > 0:
        resources_per_worker["GPU"] = args.gpus_per_worker
    trainer = TorchTrainer(
        train_loop_per_worker=train_loop_per_worker,
        train_loop_config=train_loop_config,
        scaling_config=ScalingConfig(
            num_workers=max(1, args.num_workers),
            use_gpu=args.gpus_per_worker > 0,
            resources_per_worker=resources_per_worker,
        ),
        run_config=RunConfig(storage_path=str(output_dir / "ray_train_runs")),
    )
    result = trainer.fit()
    print(json.dumps({"rayTrainResultMetrics": result.metrics}, indent=2, sort_keys=True))
    ray.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
