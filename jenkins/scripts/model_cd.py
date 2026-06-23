from __future__ import annotations

import argparse
import json
import os
import subprocess
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


def write_values(manifest: dict, output_dir: Path) -> Path:
    values = {
        "kserve": {
            "namespace": {"name": "kserve-triton-inference"},
            "inferenceService": {
                "name": "recsys-bst-triton",
                "storageUri": manifest["triton_storage_uri"],
            },
        },
        "api": {
            "namespace": {"name": "api-serving"},
            "config": {
                "modelVersion": manifest["model_version"],
            },
        },
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    target = output_dir / "recsys-serving-values.json"
    target.write_text(json.dumps(values, indent=2, sort_keys=True), encoding="utf-8")
    return target


def run(command: list[str]) -> None:
    subprocess.check_call(command)


def deploy(values_path: Path, timeout: str) -> None:
    run(["helm", "lint", "infra/helm/recsys-serving", "-f", str(values_path)])
    run(
        [
            "helm",
            "upgrade",
            "--install",
            "recsys-serving",
            "infra/helm/recsys-serving",
            "--namespace",
            "kserve-triton-inference",
            "--create-namespace",
            "--atomic",
            "--timeout",
            timeout,
            "-f",
            str(values_path),
        ]
    )
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


def main() -> int:
    parser = argparse.ArgumentParser(description="Deploy promoted RecSys Triton model to KServe")
    parser.add_argument("--manifest-uri", default=os.getenv("PROMOTION_MANIFEST_URI", "s3://recsys-model-store/promotions/bst/latest.json"))
    parser.add_argument("--output-dir", default=".model-cd")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--timeout", default="300s")
    args = parser.parse_args()

    manifest = read_manifest(args.manifest_uri)
    verify_model_repository(manifest["triton_storage_uri"])
    output_dir = Path(args.output_dir)
    values_path = write_values(manifest, output_dir)
    (output_dir / "deployed-model.json").write_text(
        json.dumps(
            {
                "model_name": manifest["model_name"],
                "model_version": manifest["model_version"],
                "triton_storage_uri": manifest["triton_storage_uri"],
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
