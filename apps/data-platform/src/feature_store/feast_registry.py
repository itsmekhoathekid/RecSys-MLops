from __future__ import annotations

import os
import subprocess
from pathlib import Path
from urllib.parse import urlparse


def feast_repo_path(repo_path: str | Path = "apps/data-platform/feature-store/feature_repo") -> Path:
    path = Path(repo_path)
    if not (path / "feature_store.yaml").exists():
        raise FileNotFoundError(f"Missing Feast feature_store.yaml in {path}")
    return path


def registry_path(repo_path: str | Path = "apps/data-platform/feature-store/feature_repo") -> Path:
    return feast_repo_path(repo_path) / "data" / "registry.db"


def registry_backup_uri() -> str | None:
    return os.getenv("FEAST_REGISTRY_BACKUP_URI", "s3://recsys-feature-store/registry/registry.db")


def _parse_s3_uri(uri: str) -> tuple[str, str]:
    parsed = urlparse(uri)
    if parsed.scheme != "s3" or not parsed.netloc or not parsed.path.strip("/"):
        raise ValueError(f"Expected s3://bucket/key registry URI, got {uri!r}")
    return parsed.netloc, parsed.path.lstrip("/")


def _s3_client():
    import boto3

    return boto3.client(
        "s3",
        endpoint_url=os.getenv("FEAST_S3_ENDPOINT", os.getenv("MINIO_ENDPOINT", "http://minio:9000")),
        aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID", os.getenv("MINIO_ROOT_USER", "minio")),
        aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY", os.getenv("MINIO_ROOT_PASSWORD", "minio123")),
        region_name=os.getenv("AWS_DEFAULT_REGION", "us-east-1"),
    )


def restore_registry_backup(
    repo_path: str | Path = "apps/data-platform/feature-store/feature_repo",
    backup_uri: str | None = None,
) -> bool:
    uri = backup_uri if backup_uri is not None else registry_backup_uri()
    if not uri:
        return False
    bucket, key = _parse_s3_uri(uri)
    target = registry_path(repo_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        _s3_client().download_file(bucket, key, str(target))
    except Exception:
        return False
    return True


def backup_registry(
    repo_path: str | Path = "apps/data-platform/feature-store/feature_repo",
    backup_uri: str | None = None,
) -> bool:
    uri = backup_uri if backup_uri is not None else registry_backup_uri()
    if not uri:
        return False
    source = registry_path(repo_path)
    if not source.exists():
        return False
    bucket, key = _parse_s3_uri(uri)
    _s3_client().upload_file(str(source), bucket, key)
    return True


def run_feast_command(args: list[str], repo_path: str | Path = "apps/data-platform/feature-store/feature_repo") -> subprocess.CompletedProcess[str]:
    path = feast_repo_path(repo_path)
    return subprocess.run(
        ["feast", *args],
        cwd=path,
        check=True,
        text=True,
        capture_output=True,
    )


def apply_feature_repo(repo_path: str | Path = "apps/data-platform/feature-store/feature_repo") -> subprocess.CompletedProcess[str]:
    return run_feast_command(["apply"], repo_path)


def materialize_incremental(end_ts: str, repo_path: str | Path = "apps/data-platform/feature-store/feature_repo") -> subprocess.CompletedProcess[str]:
    return run_feast_command(["materialize-incremental", end_ts], repo_path)


def apply_and_materialize_incremental(
    end_ts: str,
    repo_path: str | Path = "apps/data-platform/feature-store/feature_repo",
    backup_uri: str | None = None,
) -> dict[str, str | bool]:
    restored = restore_registry_backup(repo_path, backup_uri)
    apply_result = apply_feature_repo(repo_path)
    materialize_result = materialize_incremental(end_ts, repo_path)
    backed_up = backup_registry(repo_path, backup_uri)
    return {
        "registry_restored": restored,
        "registry_backed_up": backed_up,
        "apply_stdout": apply_result.stdout,
        "apply_stderr": apply_result.stderr,
        "materialize_stdout": materialize_result.stdout,
        "materialize_stderr": materialize_result.stderr,
    }
