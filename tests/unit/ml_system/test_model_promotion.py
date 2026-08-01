from __future__ import annotations

import json
from pathlib import Path

import yaml

from registry.model_promotion import (
    DEFAULT_OBJECTIVE_METRIC,
    build_triton_repository,
    promote_best_model,
)


REQUIRED_MODEL_FILES = [
    "bst_preprocess/1/model.py",
    "bst_preprocess/config.pbtxt",
    "bst_ranker/1/model.onnx",
    "bst_ranker/config.pbtxt",
    "bst_postprocess/1/model.py",
    "bst_postprocess/config.pbtxt",
    "bst_ensemble/1/.keep",
    "bst_ensemble/config.pbtxt",
]


def _config(path: Path) -> Path:
    payload = {
        "model_args": {
            "n_heads": 1,
            "k_interests": 2,
            "embed_dim": 4,
            "seq_len": 5,
            "intermediate_size": 8,
            "hidden_dropout_prob": 0.1,
            "attn_dropout_prob": 0.1,
            "hidden_act": "relu",
            "layer_norm_eps": 1e-12,
            "padding_idx": 0,
            "item_num": 10,
            "category_num": 10,
            "brand_num": 10,
            "price_bucket_num": 10,
            "time_bucket_num": 10,
            "event_type_num": 10,
        },
        "data_args": {
            "max_history_len": 5,
            "train_data_path": "train.jsonl",
            "val_data_path": "val.jsonl",
            "test_data_path": "test.jsonl",
            "padding_idx": 0,
        },
        "training_args": {
            "batch_size": 2,
            "num_workers": 0,
            "learning_rate": 0.001,
            "weight_decay": 0.0,
        },
    }
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    return path


def test_build_triton_repository_writes_expected_contract(tmp_path):
    config_path = _config(tmp_path / "bst.yaml")
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))

    repo = build_triton_repository(
        config=config,
        checkpoint_path=str(tmp_path / "missing-checkpoint.pt"),
        repository=tmp_path / "repo",
        skip_onnx_export=True,
    )

    for relative in REQUIRED_MODEL_FILES:
        assert (repo / relative).exists(), relative
    assert 'name: "bst_ensemble"' in (repo / "bst_ensemble" / "config.pbtxt").read_text(encoding="utf-8")
    ranker_config = (repo / "bst_ranker" / "config.pbtxt").read_text(encoding="utf-8")
    assert 'platform: "onnxruntime_onnx"' in ranker_config
    assert 'name: "hist_item_id" data_type: TYPE_INT64 dims: [ -1, -1 ]' in ranker_config
    assert 'name: "target_item_id" data_type: TYPE_INT64 dims: [ -1 ]' in ranker_config


def test_promote_best_model_writes_manifest_without_upload(tmp_path):
    config_path = _config(tmp_path / "bst.yaml")
    ray_result = tmp_path / "best_result.json"
    ray_result.write_text(
        json.dumps(
            {
                "best_trial_name": "trial-001",
                "best_config_path": str(config_path),
                "checkpoint_path": str(tmp_path / "checkpoint.pt"),
                "artifact_uri": "s3://mlflow-artifacts/run/artifacts/model",
                "mlflow_run_id": "run-1",
                "metrics": {"test/ndcg@10": 0.42},
            }
        ),
        encoding="utf-8",
    )

    result = promote_best_model(
        ray_result_path=str(ray_result),
        config_path=str(config_path),
        output_dir=str(tmp_path / "serving"),
        model_bucket="recsys-model-store",
        model_prefix="triton/bst",
        promotion_key="promotions/bst/latest.json",
        metric_name=DEFAULT_OBJECTIVE_METRIC,
        upload=False,
        skip_onnx_export=True,
    )

    assert result.manifest["model_version"] == "trial-001"
    assert result.manifest["metric_value"] == 0.42
    assert result.manifest["triton_storage_uri"] == "s3://recsys-model-store/triton/bst/trial-001"
    assert result.manifest["serving_storage_uri"] == "s3://recsys-model-store/triton/bst/trial-001"
    assert result.manifest["promotion_manifest_uri"] == "s3://recsys-model-store/promotions/bst/trial-001.json"


def test_promote_best_model_uses_held_out_test_metric(tmp_path):
    config_path = _config(tmp_path / "bst.yaml")
    ray_result = tmp_path / "best_result.json"
    ray_result.write_text(
        json.dumps(
            {
                "best_trial_name": "trial-test-metric",
                "best_config_path": str(config_path),
                "checkpoint_path": str(tmp_path / "checkpoint.pt"),
                "metrics": {"val/ndcg@10": 0.27},
            }
        ),
        encoding="utf-8",
    )
    eval_metrics = tmp_path / "eval_metrics.json"
    eval_metrics.write_text(
        json.dumps({"split": "test", "metrics": {"ndcg@10": 0.3375}}),
        encoding="utf-8",
    )

    result = promote_best_model(
        ray_result_path=str(ray_result),
        eval_metrics_path=str(eval_metrics),
        config_path=str(config_path),
        output_dir=str(tmp_path / "serving"),
        metric_name=DEFAULT_OBJECTIVE_METRIC,
        upload=False,
        skip_onnx_export=True,
    )

    assert result.manifest["metric_name"] == "test_ndcg_at_10"
    assert result.manifest["metric_value"] == 0.3375
