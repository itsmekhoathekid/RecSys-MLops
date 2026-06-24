from __future__ import annotations

import argparse
import json
import os
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import torch

from models import BST, load_config
from model_registry import register_model_config


MODEL_NAME = "bst"
RANKER_MODEL_NAME = "bst_ranker"
PREPROCESS_MODEL_NAME = "bst_preprocess"
POSTPROCESS_MODEL_NAME = "bst_postprocess"
ENSEMBLE_MODEL_NAME = "bst_ensemble"
DEFAULT_MODEL_BUCKET = "recsys-model-store"
DEFAULT_MODEL_PREFIX = "triton/bst"
DEFAULT_PROMOTION_KEY = "promotions/bst/latest.json"
DEFAULT_OBJECTIVE_METRIC = "test_ndcg_at_10"

HISTORY_INPUTS = [
    "hist_item_id",
    "hist_event_type",
    "hist_category",
    "hist_brand",
    "hist_price_bucket",
    "hist_time",
]

TARGET_INPUTS = [
    "target_item_id",
    "target_category",
    "target_brand",
    "target_price_bucket",
]

ONNX_INPUTS = [*HISTORY_INPUTS, *TARGET_INPUTS]
RANKER_HISTORY_OUTPUTS = {name: f"ranker_{name}" for name in HISTORY_INPUTS}


@dataclass(frozen=True)
class PromotionResult:
    model_version: str
    local_model_repository: str
    triton_storage_uri: str
    promotion_manifest_uri: str
    manifest: dict[str, Any]


def metric_to_triton_name(name: str) -> str:
    return name.replace("/", "_").replace("@", "_at_")


def s3_uri(bucket: str, key: str) -> str:
    return f"s3://{bucket}/{key.strip('/')}"


def _read_json(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _best_metric(payload: dict[str, Any], metric_name: str) -> float:
    metrics = payload.get("metrics") or {}
    candidates = [metric_name]
    if metric_name.startswith("test_"):
        candidates.append(metric_name.removeprefix("test_").replace("_at_", "@").replace("_", "/"))
    if metric_name.startswith("val_"):
        candidates.append(metric_name.removeprefix("val_").replace("_at_", "@").replace("_", "/"))
    candidates.extend(["test/ndcg@10", "test_ndcg_at_10", "val/ndcg@10", "val_ndcg_at_10", "best_score"])
    for name in candidates:
        if name in metrics and isinstance(metrics[name], (int, float)):
            return float(metrics[name])
    ray_metrics = payload.get("ray_metrics") or {}
    for name in candidates:
        if name in ray_metrics and isinstance(ray_metrics[name], (int, float)):
            return float(ray_metrics[name])
    return 0.0


def _load_model(config: dict[str, Any], checkpoint_path: str) -> BST:
    model = BST(config["model_args"])
    checkpoint = torch.load(checkpoint_path, map_location="cpu")
    model.load_state_dict(checkpoint["model_state_dict"])
    model.eval()
    return model


def sample_onnx_inputs(config: dict[str, Any], batch_size: int = 2) -> tuple[torch.Tensor, ...]:
    data_args = config["data_args"]
    max_history_len = int(data_args.get("max_history_len", config["model_args"].get("seq_len", 50)))
    history = tuple(torch.zeros((batch_size, max_history_len), dtype=torch.long) for _ in HISTORY_INPUTS)
    target = tuple(torch.zeros((batch_size,), dtype=torch.long) for _ in TARGET_INPUTS)
    return (*history, *target)


def export_onnx(config: dict[str, Any], checkpoint_path: str, target_path: str | Path) -> None:
    target = Path(target_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    model = _load_model(config, checkpoint_path)
    dynamic_axes = {name: {0: "batch"} for name in ONNX_INPUTS}
    for name in HISTORY_INPUTS:
        dynamic_axes[name][1] = "history_len"
    dynamic_axes["logits"] = {0: "batch"}
    torch.onnx.export(
        model,
        sample_onnx_inputs(config),
        str(target),
        input_names=ONNX_INPUTS,
        output_names=["logits"],
        dynamic_axes=dynamic_axes,
        opset_version=17,
        dynamo=False,
    )


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text.strip() + "\n", encoding="utf-8")


def _pbtxt_list(entries: list[str]) -> str:
    return ",\n".join(f"  {{ {entry} }}" for entry in entries)


def write_triton_configs(repository: str | Path, max_history_len: int) -> None:
    repo = Path(repository)
    ensemble_history_input_entries = [
        f'name: "{name}" data_type: TYPE_INT64 dims: [ -1 ]' for name in HISTORY_INPUTS
    ]
    ranker_history_entries = [
        f'name: "{name}" data_type: TYPE_INT64 dims: [ -1, -1 ]' for name in HISTORY_INPUTS
    ]
    ranker_target_entries = [
        f'name: "{name}" data_type: TYPE_INT64 dims: [ -1 ]' for name in TARGET_INPUTS
    ]
    ensemble_input_entries = [
        'name: "candidate_item_id" data_type: TYPE_INT64 dims: [ -1 ]',
        'name: "candidate_category" data_type: TYPE_INT64 dims: [ -1 ]',
        'name: "candidate_brand" data_type: TYPE_INT64 dims: [ -1 ]',
        'name: "candidate_price_bucket" data_type: TYPE_INT64 dims: [ -1 ]',
        *ensemble_history_input_entries,
    ]
    ensemble_output_entries = [
        'name: "candidate_item_id_out" data_type: TYPE_INT64 dims: [ -1 ]',
        'name: "score" data_type: TYPE_FP32 dims: [ -1 ]',
    ]
    preprocess_output_entries = [
        *[
            f'name: "{RANKER_HISTORY_OUTPUTS[name]}" data_type: TYPE_INT64 dims: [ -1, -1 ]'
            for name in HISTORY_INPUTS
        ],
        *ranker_target_entries,
        'name: "candidate_item_id_out" data_type: TYPE_INT64 dims: [ -1 ]',
    ]
    ranker_input_entries = [
        *ranker_history_entries,
        *ranker_target_entries,
    ]
    ensemble_input_blocks = _pbtxt_list(ensemble_input_entries)
    ensemble_output_blocks = _pbtxt_list(ensemble_output_entries)
    preprocess_output_blocks = _pbtxt_list(preprocess_output_entries)
    ranker_input_blocks = _pbtxt_list(ranker_input_entries)
    postprocess_input_blocks = _pbtxt_list(
        [
            'name: "logits" data_type: TYPE_FP32 dims: [ -1 ]',
        ]
    )
    _write_text(
        repo / PREPROCESS_MODEL_NAME / "config.pbtxt",
        f"""
name: "{PREPROCESS_MODEL_NAME}"
backend: "python"
max_batch_size: 0
input [
{ensemble_input_blocks}
]
output [
{preprocess_output_blocks}
]
parameters: {{
  key: "max_history_len"
  value: {{ string_value: "{max_history_len}" }}
}}
instance_group [
  {{ kind: KIND_CPU }}
]
""",
    )
    _write_text(
        repo / RANKER_MODEL_NAME / "config.pbtxt",
        f"""
name: "{RANKER_MODEL_NAME}"
platform: "onnxruntime_onnx"
max_batch_size: 0
input [
{ranker_input_blocks}
]
output [
  {{ name: "logits" data_type: TYPE_FP32 dims: [ -1 ] }}
]
""",
    )
    _write_text(
        repo / POSTPROCESS_MODEL_NAME / "config.pbtxt",
        f"""
name: "{POSTPROCESS_MODEL_NAME}"
backend: "python"
max_batch_size: 0
input [
{postprocess_input_blocks}
]
output [
  {{ name: "score" data_type: TYPE_FP32 dims: [ -1 ] }}
]
instance_group [
  {{ kind: KIND_CPU }}
]
""",
    )
    _write_text(
        repo / ENSEMBLE_MODEL_NAME / "config.pbtxt",
        f"""
name: "{ENSEMBLE_MODEL_NAME}"
platform: "ensemble"
max_batch_size: 0
input [
{ensemble_input_blocks}
]
output [
{ensemble_output_blocks}
]
ensemble_scheduling {{
  step [
    {{
      model_name: "{PREPROCESS_MODEL_NAME}"
      model_version: -1
      input_map {{
        key: "candidate_item_id"
        value: "candidate_item_id"
      }}
      input_map {{
        key: "candidate_category"
        value: "candidate_category"
      }}
      input_map {{
        key: "candidate_brand"
        value: "candidate_brand"
      }}
      input_map {{
        key: "candidate_price_bucket"
        value: "candidate_price_bucket"
      }}
{_ensemble_history_maps()}
{_ensemble_preprocess_output_maps()}
    }},
    {{
      model_name: "{RANKER_MODEL_NAME}"
      model_version: -1
{_ranker_input_maps()}
      output_map {{
        key: "logits"
        value: "logits"
      }}
    }},
    {{
      model_name: "{POSTPROCESS_MODEL_NAME}"
      model_version: -1
      input_map {{
        key: "logits"
        value: "logits"
      }}
      output_map {{
        key: "score"
        value: "score"
      }}
    }}
  ]
}}
""",
    )


def _ensemble_history_maps() -> str:
    return "\n".join(
        f"""      input_map {{
        key: "{name}"
        value: "{name}"
      }}"""
        for name in HISTORY_INPUTS
    )


def _ensemble_preprocess_output_maps() -> str:
    return "\n".join(
        f"""      output_map {{
        key: "{RANKER_HISTORY_OUTPUTS.get(name, name)}"
        value: "{RANKER_HISTORY_OUTPUTS.get(name, name)}"
      }}"""
        for name in [*ONNX_INPUTS, "candidate_item_id_out"]
    )


def _ranker_input_maps() -> str:
    return "\n".join(
        f"""      input_map {{
        key: "{name}"
        value: "{RANKER_HISTORY_OUTPUTS.get(name, name)}"
      }}"""
        for name in ONNX_INPUTS
    )


PREPROCESS_MODEL = r'''
import json
import numpy as np
import triton_python_backend_utils as pb_utils


HISTORY_INPUTS = [
    "hist_item_id",
    "hist_event_type",
    "hist_category",
    "hist_brand",
    "hist_price_bucket",
    "hist_time",
]


class TritonPythonModel:
    def initialize(self, args):
        config = json.loads(args["model_config"])
        params = config.get("parameters", {})
        self.max_history_len = int(params.get("max_history_len", {}).get("string_value", "50"))

    def _history(self, request, name, n_candidates):
        value = pb_utils.get_input_tensor_by_name(request, name).as_numpy().astype(np.int64).reshape(-1)
        value = value[-self.max_history_len:]
        if value.size < self.max_history_len:
            value = np.pad(value, (self.max_history_len - value.size, 0), constant_values=0)
        value = np.maximum(value, 0)
        return np.repeat(value.reshape(1, self.max_history_len), n_candidates, axis=0)

    def _target(self, request, name):
        return np.maximum(pb_utils.get_input_tensor_by_name(request, name).as_numpy().astype(np.int64).reshape(-1), 0)

    def execute(self, requests):
        responses = []
        for request in requests:
            candidate_item_id = self._target(request, "candidate_item_id")
            n_candidates = candidate_item_id.size
            outputs = [
                pb_utils.Tensor(f"ranker_{name}", self._history(request, name, n_candidates))
                for name in HISTORY_INPUTS
            ]
            outputs.extend(
                [
                    pb_utils.Tensor("target_item_id", candidate_item_id),
                    pb_utils.Tensor("target_category", self._target(request, "candidate_category")),
                    pb_utils.Tensor("target_brand", self._target(request, "candidate_brand")),
                    pb_utils.Tensor("target_price_bucket", self._target(request, "candidate_price_bucket")),
                    pb_utils.Tensor("candidate_item_id_out", candidate_item_id),
                ]
            )
            responses.append(pb_utils.InferenceResponse(output_tensors=outputs))
        return responses
'''


POSTPROCESS_MODEL = r'''
import numpy as np
import triton_python_backend_utils as pb_utils


class TritonPythonModel:
    def execute(self, requests):
        responses = []
        for request in requests:
            logits = pb_utils.get_input_tensor_by_name(request, "logits").as_numpy().astype(np.float32).reshape(-1)
            scores = 1.0 / (1.0 + np.exp(-logits))
            responses.append(
                pb_utils.InferenceResponse(
                    output_tensors=[
                        pb_utils.Tensor("score", scores.astype(np.float32)),
                    ]
                )
            )
        return responses
'''


def write_python_backend_models(repository: str | Path) -> None:
    repo = Path(repository)
    _write_text(repo / PREPROCESS_MODEL_NAME / "1" / "model.py", PREPROCESS_MODEL)
    _write_text(repo / POSTPROCESS_MODEL_NAME / "1" / "model.py", POSTPROCESS_MODEL)


def build_triton_repository(
    config: dict[str, Any],
    checkpoint_path: str,
    repository: str | Path,
    skip_onnx_export: bool = False,
) -> Path:
    repo = Path(repository)
    if repo.exists():
        shutil.rmtree(repo)
    (repo / RANKER_MODEL_NAME / "1").mkdir(parents=True, exist_ok=True)
    _write_text(repo / ENSEMBLE_MODEL_NAME / "1" / ".keep", "ensemble version directory")
    write_python_backend_models(repo)
    write_triton_configs(
        repo,
        max_history_len=int(config["data_args"].get("max_history_len", config["model_args"].get("seq_len", 50))),
    )
    if skip_onnx_export:
        (repo / RANKER_MODEL_NAME / "1" / "model.onnx").write_bytes(b"")
    else:
        export_onnx(config, checkpoint_path, repo / RANKER_MODEL_NAME / "1" / "model.onnx")
    return repo


def upload_directory_to_s3(local_dir: str | Path, bucket: str, prefix: str) -> None:
    import boto3

    client = boto3.client(
        "s3",
        endpoint_url=os.getenv("MINIO_ENDPOINT") or os.getenv("MLFLOW_S3_ENDPOINT_URL"),
        aws_access_key_id=os.getenv("MINIO_ROOT_USER") or os.getenv("AWS_ACCESS_KEY_ID"),
        aws_secret_access_key=os.getenv("MINIO_ROOT_PASSWORD") or os.getenv("AWS_SECRET_ACCESS_KEY"),
        region_name=os.getenv("AWS_DEFAULT_REGION", "us-east-1"),
    )
    try:
        client.create_bucket(Bucket=bucket)
    except Exception:
        pass
    root = Path(local_dir)
    for path in root.rglob("*"):
        if path.is_file():
            key = f"{prefix.strip('/')}/{path.relative_to(root).as_posix()}"
            client.upload_file(str(path), bucket, key)


def upload_manifest(manifest: dict[str, Any], bucket: str, key: str) -> None:
    import boto3

    client = boto3.client(
        "s3",
        endpoint_url=os.getenv("MINIO_ENDPOINT") or os.getenv("MLFLOW_S3_ENDPOINT_URL"),
        aws_access_key_id=os.getenv("MINIO_ROOT_USER") or os.getenv("AWS_ACCESS_KEY_ID"),
        aws_secret_access_key=os.getenv("MINIO_ROOT_PASSWORD") or os.getenv("AWS_SECRET_ACCESS_KEY"),
        region_name=os.getenv("AWS_DEFAULT_REGION", "us-east-1"),
    )
    try:
        client.create_bucket(Bucket=bucket)
    except Exception:
        pass
    client.put_object(
        Bucket=bucket,
        Key=key.strip("/"),
        Body=json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8"),
        ContentType="application/json",
    )


def build_manifest(
    best_payload: dict[str, Any],
    config: dict[str, Any],
    model_version: str,
    metric_name: str,
    triton_storage_uri: str,
    serving_storage_uri: str,
    promotion_manifest_uri: str,
) -> dict[str, Any]:
    triton_metric_name = metric_to_triton_name(metric_name)
    return {
        "model_name": MODEL_NAME,
        "model_version": model_version,
        "metric_name": triton_metric_name,
        "metric_value": _best_metric(best_payload, triton_metric_name),
        "mlflow_run_id": best_payload.get("mlflow_run_id"),
        "source_checkpoint_uri": best_payload.get("artifact_uri") or best_payload.get("checkpoint_path"),
        "source_checkpoint_path": best_payload.get("checkpoint_path"),
        "triton_storage_uri": triton_storage_uri,
        "serving_storage_uri": serving_storage_uri,
        "promotion_manifest_uri": promotion_manifest_uri,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "tensor_schema": {
            "history_inputs": HISTORY_INPUTS,
            "target_inputs": TARGET_INPUTS,
            "ensemble_inputs": [
                "candidate_item_id",
                "candidate_category",
                "candidate_brand",
                "candidate_price_bucket",
                *HISTORY_INPUTS,
            ],
            "ensemble_outputs": ["candidate_item_id_out", "score"],
            "dtype": "INT64",
            "max_history_len": int(config["data_args"].get("max_history_len", 50)),
        },
    }


def promote_best_model(
    ray_result_path: str,
    config_path: str,
    output_dir: str,
    model_bucket: str = DEFAULT_MODEL_BUCKET,
    model_prefix: str = DEFAULT_MODEL_PREFIX,
    promotion_key: str = DEFAULT_PROMOTION_KEY,
    metric_name: str = DEFAULT_OBJECTIVE_METRIC,
    model_version: str | None = None,
    upload: bool = True,
    skip_onnx_export: bool = False,
    manifest_path: str | None = None,
) -> PromotionResult:
    best_payload = _read_json(ray_result_path)
    config = load_config(best_payload.get("best_config_path") or config_path)
    version = model_version or os.getenv("MODEL_VERSION") or best_payload.get("best_trial_name")
    if not version:
        version = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    repo_path = Path(output_dir) / "triton_model_repository" / MODEL_NAME / version
    build_triton_repository(
        config=config,
        checkpoint_path=best_payload["checkpoint_path"],
        repository=repo_path,
        skip_onnx_export=skip_onnx_export,
    )
    model_prefix = model_prefix.strip("/")
    storage_prefix = f"{model_prefix}/{version}"
    serving_prefix = f"{model_prefix}/latest"
    triton_uri = s3_uri(model_bucket, storage_prefix)
    serving_uri = s3_uri(model_bucket, serving_prefix)
    manifest_uri = s3_uri(model_bucket, promotion_key)
    manifest = build_manifest(
        best_payload=best_payload,
        config=config,
        model_version=version,
        metric_name=metric_name,
        triton_storage_uri=triton_uri,
        serving_storage_uri=serving_uri,
        promotion_manifest_uri=manifest_uri,
    )
    local_manifest = Path(manifest_path or Path(output_dir) / "promotion_manifest.json")
    local_manifest.parent.mkdir(parents=True, exist_ok=True)
    local_manifest.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    if upload:
        upload_directory_to_s3(repo_path, model_bucket, storage_prefix)
        if serving_prefix != storage_prefix:
            upload_directory_to_s3(repo_path, model_bucket, serving_prefix)
        upload_manifest(manifest, model_bucket, promotion_key)
        versioned_key = f"promotions/bst/{version}.json"
        upload_manifest(manifest, model_bucket, versioned_key)
    postgres_uri = os.getenv("MODEL_REGISTRY_POSTGRES_URI") or os.getenv("POSTGRES_MODEL_REGISTRY_URI")
    if postgres_uri:
        register_model_config(
            postgres_uri=postgres_uri,
            model_name=MODEL_NAME,
            model_version=version,
            artifact_uri=triton_uri,
            mlflow_run_id=best_payload.get("mlflow_run_id"),
            metrics={manifest["metric_name"]: manifest["metric_value"]},
            config=config,
            serving_artifact_uri=serving_uri,
            promotion_manifest_uri=manifest_uri,
        )
    return PromotionResult(
        model_version=version,
        local_model_repository=str(repo_path),
        triton_storage_uri=triton_uri,
        promotion_manifest_uri=manifest_uri,
        manifest=manifest,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Export and promote the best BST model to Triton/MinIO")
    parser.add_argument("--ray-result-path", required=True)
    parser.add_argument("--config-path", default="configs/local/bst.yaml")
    parser.add_argument("--output-dir", default="/workspace/recsys/data_platform/output/ml/serving")
    parser.add_argument("--model-bucket", default=os.getenv("MODEL_STORE_BUCKET", DEFAULT_MODEL_BUCKET))
    parser.add_argument("--model-prefix", default=os.getenv("MODEL_STORE_PREFIX", DEFAULT_MODEL_PREFIX))
    parser.add_argument("--promotion-key", default=os.getenv("PROMOTION_MANIFEST_KEY", DEFAULT_PROMOTION_KEY))
    parser.add_argument("--metric-name", default=os.getenv("PROMOTION_METRIC_NAME", DEFAULT_OBJECTIVE_METRIC))
    parser.add_argument("--model-version", default=os.getenv("MODEL_VERSION", ""))
    parser.add_argument("--manifest-path", default="")
    parser.add_argument("--no-upload", action="store_true")
    parser.add_argument("--skip-onnx-export", action="store_true")
    args = parser.parse_args()
    result = promote_best_model(
        ray_result_path=args.ray_result_path,
        config_path=args.config_path,
        output_dir=args.output_dir,
        model_bucket=args.model_bucket,
        model_prefix=args.model_prefix,
        promotion_key=args.promotion_key,
        metric_name=args.metric_name,
        model_version=args.model_version or None,
        upload=not args.no_upload,
        skip_onnx_export=args.skip_onnx_export,
        manifest_path=args.manifest_path or None,
    )
    print(json.dumps(result.manifest, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
