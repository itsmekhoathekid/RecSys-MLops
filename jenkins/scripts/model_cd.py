from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
import urllib.parse
import urllib.request
from pathlib import Path
from urllib.parse import urlparse


REQUIRED_MODEL_FILES = [
    "bst_preprocess/1/model.py",
    "bst_preprocess/config.pbtxt",
    "bst_ranker/1/model.onnx",
    "bst_ranker/config.pbtxt",
    "bst_postprocess/1/model.py",
    "bst_postprocess/config.pbtxt",
    "bst_ensemble/config.pbtxt",
]


def s3_client():
    import boto3

    return boto3.client(
        "s3",
        endpoint_url=os.getenv("MINIO_ENDPOINT") or os.getenv("MLFLOW_S3_ENDPOINT_URL"),
        aws_access_key_id=os.getenv("MINIO_ROOT_USER") or os.getenv("AWS_ACCESS_KEY_ID"),
        aws_secret_access_key=os.getenv("MINIO_ROOT_PASSWORD") or os.getenv("AWS_SECRET_ACCESS_KEY"),
        region_name=os.getenv("AWS_DEFAULT_REGION", "us-east-1"),
    )


def parse_s3_uri(uri: str) -> tuple[str, str]:
    parsed = urlparse(uri)
    if parsed.scheme != "s3" or not parsed.netloc:
        raise ValueError(f"Expected s3:// URI, got {uri}")
    return parsed.netloc, parsed.path.strip("/")


def read_manifest(uri: str) -> dict:
    if uri.startswith("s3://"):
        bucket, key = parse_s3_uri(uri)
        response = s3_client().get_object(Bucket=bucket, Key=key)
        return json.loads(response["Body"].read().decode("utf-8"))
    return json.loads(Path(uri).read_text(encoding="utf-8"))


def verify_model_repository(storage_uri: str) -> None:
    missing = []
    if storage_uri.startswith("s3://"):
        bucket, prefix = parse_s3_uri(storage_uri)
        client = s3_client()
        for relative in REQUIRED_MODEL_FILES:
            key = f"{prefix.rstrip('/')}/{relative}"
            try:
                client.head_object(Bucket=bucket, Key=key)
            except Exception:
                missing.append(f"s3://{bucket}/{key}")
    else:
        root = Path(storage_uri)
        missing = [str(root / relative) for relative in REQUIRED_MODEL_FILES if not (root / relative).exists()]
    if missing:
        raise FileNotFoundError("Missing Triton model repository files: " + ", ".join(missing))


def latest_storage_uri(control_manifest: dict | None, candidate_manifest: dict) -> str:
    if control_manifest and control_manifest.get("serving_storage_uri"):
        return control_manifest["serving_storage_uri"]
    bucket = os.getenv("MODEL_STORE_BUCKET", "recsys-model-store")
    prefix = os.getenv("MODEL_STORE_PREFIX", "triton/bst").strip("/")
    return f"s3://{bucket}/{prefix}/latest"


def copy_s3_prefix(source_uri: str, target_uri: str) -> None:
    source_bucket, source_prefix = parse_s3_uri(source_uri)
    target_bucket, target_prefix = parse_s3_uri(target_uri)
    client = s3_client()
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=source_bucket, Prefix=source_prefix.rstrip("/") + "/"):
        for item in page.get("Contents", []):
            source_key = item["Key"]
            relative = source_key[len(source_prefix.rstrip("/") + "/") :]
            if not relative:
                continue
            target_key = f"{target_prefix.rstrip('/')}/{relative}"
            if source_bucket == target_bucket and source_key == target_key:
                continue
            client.copy_object(
                Bucket=target_bucket,
                Key=target_key,
                CopySource={"Bucket": source_bucket, "Key": source_key},
            )


def upload_manifest(manifest: dict, uri: str) -> None:
    bucket, key = parse_s3_uri(uri)
    s3_client().put_object(
        Bucket=bucket,
        Key=key,
        Body=json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8"),
        ContentType="application/json",
    )


def query_prometheus(prometheus_url: str, query: str) -> float:
    encoded = urllib.parse.urlencode({"query": query})
    with urllib.request.urlopen(f"{prometheus_url.rstrip('/')}/api/v1/query?{encoded}", timeout=15) as response:
        payload = json.loads(response.read().decode("utf-8"))
    result = payload.get("data", {}).get("result", [])
    if not result:
        return 0.0
    return float(result[0]["value"][1])


def assert_promote_gates(prometheus_url: str, gate_window: str) -> None:
    if not prometheus_url:
        return
    candidate_error = query_prometheus(
        prometheus_url,
        f'sum(rate(model_predictions_total{{ab_variant="candidate",status="error"}}[{gate_window}])) '
        f'/ clamp_min(sum(rate(model_predictions_total{{ab_variant="candidate"}}[{gate_window}])), 0.001)',
    )
    control_error = query_prometheus(
        prometheus_url,
        f'sum(rate(model_predictions_total{{ab_variant="control",status="error"}}[{gate_window}])) '
        f'/ clamp_min(sum(rate(model_predictions_total{{ab_variant="control"}}[{gate_window}])), 0.001)',
    )
    candidate_latency = query_prometheus(
        prometheus_url,
        "histogram_quantile(0.95, "
        f'sum(rate(model_prediction_latency_seconds_bucket{{ab_variant="candidate"}}[{gate_window}])) by (le))',
    )
    control_latency = query_prometheus(
        prometheus_url,
        "histogram_quantile(0.95, "
        f'sum(rate(model_prediction_latency_seconds_bucket{{ab_variant="control"}}[{gate_window}])) by (le))',
    )
    if candidate_error > control_error + 0.02:
        raise RuntimeError(f"candidate error gate failed: candidate={candidate_error}, control={control_error}")
    if control_latency > 0 and candidate_latency > control_latency * 1.5:
        raise RuntimeError(f"candidate latency gate failed: candidate={candidate_latency}, control={control_latency}")


def write_values(
    manifest: dict,
    output_dir: Path,
    *,
    control_manifest: dict | None = None,
    candidate_manifest: dict | None = None,
    stage: str = "deploy",
    candidate_weight_percent: int = 0,
    experiment_id: str = "",
) -> Path:
    control = control_manifest or manifest
    candidate = candidate_manifest
    ab_enabled = stage in {"ab-start", "ab-step"} and candidate is not None and candidate_weight_percent > 0
    values = {
        "kserve": {
            "namespace": {"name": "kserve-triton-inference"},
            "inferenceService": {
                "name": "recsys-bst-triton",
                "storageUri": control["triton_storage_uri"],
            },
        },
        "api": {
            "namespace": {"name": "api-serving"},
            "config": {
                "modelVersion": control["model_version"],
            },
        },
        "abTest": {
            "enabled": ab_enabled,
            "experimentId": experiment_id,
            "candidateWeightPercent": candidate_weight_percent if ab_enabled else 0,
            "controlModelVersion": control["model_version"],
            "candidateModelVersion": candidate["model_version"] if candidate else "",
        },
    }
    if candidate is not None:
        values["kserve"]["inferenceService"]["candidateStorageUri"] = candidate["triton_storage_uri"]
    output_dir.mkdir(parents=True, exist_ok=True)
    target = output_dir / "recsys-serving-values.json"
    target.write_text(json.dumps(values, indent=2, sort_keys=True), encoding="utf-8")
    return target


def run(command: list[str]) -> None:
    subprocess.check_call(command)


def crd_exists(name: str) -> bool:
    return (
        subprocess.run(
            ["kubectl", "get", "crd", name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        ).returncode
        == 0
    )


def deploy(values_path: Path, timeout: str) -> None:
    run(["helm", "lint", "infra/helm/recsys-serving", "-f", str(values_path)])
    set_args = ["--set", "autoscaling.kserveResource.enabled=false"]
    if not crd_exists("servicemonitors.monitoring.coreos.com"):
        set_args.extend(["--set", "observability.serviceMonitor.enabled=false"])
    atomic_enabled = os.getenv("RECSYS_MODEL_CD_ATOMIC", "1").lower() not in {"0", "false", "no"}
    base_command = [
        "helm",
        "upgrade",
        "--install",
        "recsys-serving",
        "infra/helm/recsys-serving",
        "--namespace",
        "kserve-triton-inference",
        "--create-namespace",
        "--reuse-values",
        "--timeout",
        timeout,
        "-f",
        str(values_path),
    ]
    if atomic_enabled:
        base_command.insert(8, "--atomic")
    run(base_command + set_args)
    run(
        [
            "kubectl",
            "wait",
            "--for=condition=Ready",
            "inferenceservice/recsys-bst-triton",
            "-n",
            "kserve-triton-inference",
            f"--timeout={timeout}",
        ]
    )
    run(
        [
            "kubectl",
            "wait",
            "--for=condition=Available",
            "deployment/recsys-bst-triton-predictor",
            "-n",
            "kserve-triton-inference",
            f"--timeout={timeout}",
        ]
    )
    run(base_command + set_args)


def stage_manifests(args: argparse.Namespace) -> tuple[dict, dict | None]:
    control_uri = args.control_manifest_uri or args.manifest_uri
    candidate_uri = args.candidate_manifest_uri or args.manifest_uri
    control_manifest = read_manifest(control_uri)
    candidate_manifest = read_manifest(candidate_uri) if args.stage in {"ab-start", "ab-step", "promote"} else None
    return control_manifest, candidate_manifest


def main() -> int:
    parser = argparse.ArgumentParser(description="Deploy promoted RecSys Triton model to KServe")
    parser.add_argument("--manifest-uri", default=os.getenv("PROMOTION_MANIFEST_URI", "s3://recsys-model-store/promotions/bst/latest.json"))
    parser.add_argument("--control-manifest-uri", default="")
    parser.add_argument("--candidate-manifest-uri", default="")
    parser.add_argument("--candidate-weight-percent", type=int, default=10)
    parser.add_argument("--experiment-id", default="")
    parser.add_argument("--stage", choices=["deploy", "ab-start", "ab-step", "promote", "rollback"], default="deploy")
    parser.add_argument("--prometheus-url", default="")
    parser.add_argument("--gate-window", default="10m")
    parser.add_argument("--output-dir", default=".model-cd")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--timeout", default="300s")
    args = parser.parse_args()

    manifest, candidate_manifest = stage_manifests(args)
    skip_final_verify = False
    if args.stage == "promote":
        if not candidate_manifest:
            raise ValueError("--candidate-manifest-uri is required for promote")
        verify_model_repository(candidate_manifest["triton_storage_uri"])
        assert_promote_gates(args.prometheus_url, args.gate_window)
        serving_uri = latest_storage_uri(manifest, candidate_manifest)
        if args.apply:
            copy_s3_prefix(candidate_manifest["triton_storage_uri"], serving_uri)
            promoted_manifest = dict(candidate_manifest)
            promoted_manifest["serving_storage_uri"] = serving_uri
            promoted_manifest["promotion_manifest_uri"] = args.manifest_uri
            upload_manifest(promoted_manifest, args.manifest_uri)
        manifest = dict(candidate_manifest)
        manifest["triton_storage_uri"] = serving_uri
        manifest["serving_storage_uri"] = serving_uri
        manifest["promotion_manifest_uri"] = args.manifest_uri
        candidate_manifest = None
        args.stage = "deploy"
        args.candidate_weight_percent = 0
        skip_final_verify = not args.apply
    if not skip_final_verify:
        verify_model_repository(manifest["triton_storage_uri"])
    if candidate_manifest:
        verify_model_repository(candidate_manifest["triton_storage_uri"])
    output_dir = Path(args.output_dir)
    experiment_id = args.experiment_id or f"bst-{int(time.time())}"
    values_path = write_values(
        manifest,
        output_dir,
        control_manifest=manifest,
        candidate_manifest=candidate_manifest,
        stage=args.stage,
        candidate_weight_percent=max(0, min(100, args.candidate_weight_percent)),
        experiment_id=experiment_id,
    )
    (output_dir / "deployed-model.json").write_text(
        json.dumps(
            {
                "model_name": manifest["model_name"],
                "model_version": manifest["model_version"],
                "triton_storage_uri": manifest["triton_storage_uri"],
                "stage": args.stage,
                "candidate_model_version": candidate_manifest.get("model_version") if candidate_manifest else "",
                "candidate_weight_percent": args.candidate_weight_percent if candidate_manifest else 0,
                "experiment_id": experiment_id if candidate_manifest else "",
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    if args.apply:
        deploy(values_path, args.timeout)
    print(values_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
