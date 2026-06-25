from __future__ import annotations

import shutil
from pathlib import Path


def copy_run_to_minio_layout(local_run_path: str | Path, lake_root: str | Path, run_id: str | None = None) -> Path:
    """Copy a local generator run into the local MinIO-mounted lake layout.

    This local helper mirrors s3://recsys-lakehouse/raw/<run_id>/... without needing
    an S3 client during unit tests.
    """
    source = Path(local_run_path)
    if not source.exists():
        raise FileNotFoundError(source)
    target_run_id = run_id or source.name
    destination = Path(lake_root) / "raw" / target_run_id
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(source, destination)
    return destination


def upload_run_to_minio(
    local_run_path: str | Path,
    bucket: str = "recsys-lakehouse",
    prefix: str = "raw",
    run_id: str | None = None,
    endpoint_url: str = "http://minio:9000",
    access_key: str = "minio",
    secret_key: str = "minio123",
) -> list[str]:
    """Upload a generated run to a real S3-compatible MinIO bucket."""
    import boto3

    source = Path(local_run_path)
    if not source.exists():
        raise FileNotFoundError(source)
    target_run_id = run_id or source.name
    client = boto3.client(
        "s3",
        endpoint_url=endpoint_url,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name="us-east-1",
    )
    uploaded: list[str] = []
    for path in source.rglob("*"):
        if not path.is_file():
            continue
        key = f"{prefix.strip('/')}/{target_run_id}/{path.relative_to(source).as_posix()}"
        client.upload_file(str(path), bucket, key)
        uploaded.append(f"s3://{bucket}/{key}")
    return uploaded
