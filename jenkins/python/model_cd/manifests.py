from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import urlparse

from jenkins.python.model_cd import storage as transactional_storage

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
        endpoint_url=os.getenv("MODEL_STORE_ENDPOINT") or os.getenv("MLFLOW_S3_ENDPOINT_URL") or os.getenv("MINIO_ENDPOINT"),
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
    transactional_storage.copy_prefix(
        source_uri,
        target_uri,
        os.getenv("MODEL_CD_TRANSACTION_STATE", ""),
        client=s3_client(),
    )


def upload_manifest(manifest: dict, uri: str) -> None:
    transactional_storage.put_manifest(
        manifest,
        uri,
        os.getenv("MODEL_CD_TRANSACTION_STATE", ""),
        client=s3_client(),
    )
