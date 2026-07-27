from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


def s3_client() -> Any:
    import boto3

    return boto3.client(
        "s3",
        endpoint_url=os.getenv("MODEL_STORE_ENDPOINT")
        or os.getenv("MLFLOW_S3_ENDPOINT_URL")
        or os.getenv("MINIO_ENDPOINT"),
        aws_access_key_id=os.getenv("MINIO_ROOT_USER") or os.getenv("AWS_ACCESS_KEY_ID"),
        aws_secret_access_key=os.getenv("MINIO_ROOT_PASSWORD")
        or os.getenv("AWS_SECRET_ACCESS_KEY"),
        region_name=os.getenv("AWS_DEFAULT_REGION", "us-east-1"),
    )


def parse_s3_uri(uri: str) -> tuple[str, str]:
    parsed = urlparse(uri)
    if parsed.scheme != "s3" or not parsed.netloc:
        raise ValueError(f"Expected s3:// URI, got {uri}")
    return parsed.netloc, parsed.path.strip("/")


def require_versioning(client: Any, bucket: str) -> None:
    status = client.get_bucket_versioning(Bucket=bucket).get("Status", "")
    if status != "Enabled":
        raise RuntimeError(f"S3 bucket versioning must be Enabled before model mutation: {bucket}")


def _load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"records": [], "restored": False}
    return json.loads(path.read_text(encoding="utf-8"))


def _save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def snapshot_object(client: Any, bucket: str, key: str, state_path: str) -> None:
    require_versioning(client, bucket)
    path = Path(state_path)
    state = _load_state(path)
    if any(row["bucket"] == bucket and row["key"] == key for row in state["records"]):
        return
    try:
        response = client.head_object(Bucket=bucket, Key=key)
        record = {
            "bucket": bucket,
            "key": key,
            "existed": True,
            "versionId": response.get("VersionId", ""),
            "etag": response.get("ETag", ""),
        }
        if not record["versionId"] or record["versionId"] == "null":
            raise RuntimeError(f"versioned object has no immutable VersionId: s3://{bucket}/{key}")
    except client.exceptions.ClientError as error:
        code = str(error.response.get("Error", {}).get("Code", ""))
        if code not in {"404", "NoSuchKey", "NotFound"}:
            raise
        record = {
            "bucket": bucket,
            "key": key,
            "existed": False,
            "versionId": "",
            "etag": "",
        }
    state["records"].append(record)
    _save_state(path, state)


def copy_prefix(
    source_uri: str,
    target_uri: str,
    state_path: str = "",
    client: Any | None = None,
) -> None:
    source_bucket, source_prefix = parse_s3_uri(source_uri)
    target_bucket, target_prefix = parse_s3_uri(target_uri)
    client = client or s3_client()
    if state_path:
        require_versioning(client, target_bucket)
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(
        Bucket=source_bucket,
        Prefix=source_prefix.rstrip("/") + "/",
    ):
        for item in page.get("Contents", []):
            source_key = item["Key"]
            relative = source_key[len(source_prefix.rstrip("/") + "/") :]
            if not relative:
                continue
            target_key = f"{target_prefix.rstrip('/')}/{relative}"
            if source_bucket == target_bucket and source_key == target_key:
                continue
            if state_path:
                snapshot_object(client, target_bucket, target_key, state_path)
            client.copy_object(
                Bucket=target_bucket,
                Key=target_key,
                CopySource={"Bucket": source_bucket, "Key": source_key},
            )


def put_manifest(
    manifest: dict[str, Any],
    uri: str,
    state_path: str = "",
    client: Any | None = None,
) -> None:
    bucket, key = parse_s3_uri(uri)
    client = client or s3_client()
    if state_path:
        require_versioning(client, bucket)
    if state_path:
        snapshot_object(client, bucket, key, state_path)
    client.put_object(
        Bucket=bucket,
        Key=key,
        Body=json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8"),
        ContentType="application/json",
    )


def restore(state_path: str) -> None:
    path = Path(state_path)
    if not path.exists():
        return
    state = _load_state(path)
    client = s3_client()
    for record in reversed(state["records"]):
        bucket = record["bucket"]
        key = record["key"]
        require_versioning(client, bucket)
        if record["existed"]:
            client.copy_object(
                Bucket=bucket,
                Key=key,
                CopySource={
                    "Bucket": bucket,
                    "Key": key,
                    "VersionId": record["versionId"],
                },
            )
            restored = client.head_object(Bucket=bucket, Key=key)
            if record["etag"] and restored.get("ETag") != record["etag"]:
                raise RuntimeError(f"restored object ETag mismatch: s3://{bucket}/{key}")
        else:
            client.delete_object(Bucket=bucket, Key=key)
            try:
                client.head_object(Bucket=bucket, Key=key)
            except client.exceptions.ClientError:
                pass
            else:
                raise RuntimeError(f"new object still exists after rollback: s3://{bucket}/{key}")
    state["restored"] = True
    _save_state(path, state)


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    restore_parser = subparsers.add_parser("restore")
    restore_parser.add_argument("--state-path", required=True)
    versioning_parser = subparsers.add_parser("check-versioning")
    versioning_parser.add_argument("--uri", required=True)
    args = parser.parse_args()
    if args.command == "restore":
        restore(args.state_path)
    elif args.command == "check-versioning":
        bucket, _ = parse_s3_uri(args.uri)
        require_versioning(s3_client(), bucket)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
