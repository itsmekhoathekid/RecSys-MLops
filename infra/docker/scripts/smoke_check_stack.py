from __future__ import annotations

import argparse
import os
from dataclasses import dataclass

import boto3
import redis
import requests


@dataclass(frozen=True)
class SmokeConfig:
    minio_endpoint: str = os.getenv("MINIO_ENDPOINT", "http://minio:9000")
    minio_user: str = os.getenv("MINIO_ROOT_USER", "minio")
    minio_password: str = os.getenv("MINIO_ROOT_PASSWORD", "minio123")
    lake_bucket: str = os.getenv("LAKE_BUCKET", "recsys-lake")
    feature_store_bucket: str = os.getenv("FEATURE_STORE_BUCKET", "recsys-feature-store")
    kafka_connect_url: str = os.getenv("KAFKA_CONNECT_URL", "http://kafka-connect:8083")
    redis_host: str = os.getenv("REDIS_HOST", "redis")
    redis_port: int = int(os.getenv("REDIS_PORT", "6379"))


def s3_client(config: SmokeConfig):
    return boto3.client(
        "s3",
        endpoint_url=config.minio_endpoint,
        aws_access_key_id=config.minio_user,
        aws_secret_access_key=config.minio_password,
        region_name="us-east-1",
    )


def check_services(config: SmokeConfig) -> list[str]:
    errors: list[str] = []
    try:
        requests.get(f"{config.kafka_connect_url}/connectors", timeout=5).raise_for_status()
    except Exception as exc:  # noqa: BLE001
        errors.append(f"kafka-connect unreachable: {exc}")
    try:
        redis.Redis(host=config.redis_host, port=config.redis_port).ping()
    except Exception as exc:  # noqa: BLE001
        errors.append(f"redis unreachable: {exc}")
    try:
        s3_client(config).list_buckets()
    except Exception as exc:  # noqa: BLE001
        errors.append(f"minio unreachable: {exc}")
    return errors


def check_buckets(config: SmokeConfig) -> list[str]:
    client = s3_client(config)
    buckets = {bucket["Name"] for bucket in client.list_buckets()["Buckets"]}
    expected = {config.lake_bucket, config.feature_store_bucket}
    errors = []
    missing = expected - buckets
    if missing:
        errors.append(f"missing buckets: {sorted(missing)}")
    unexpected_project_buckets = {
        bucket for bucket in buckets if bucket.startswith("recsys-")
    } - expected
    if unexpected_project_buckets:
        errors.append(f"unexpected recsys buckets: {sorted(unexpected_project_buckets)}")
    return errors


def check_connectors(config: SmokeConfig) -> list[str]:
    errors: list[str] = []
    try:
        names = requests.get(f"{config.kafka_connect_url}/connectors", timeout=5).json()
        for name in ["recsys-postgres-cdc", "recsys-kafka-minio-raw-sink"]:
            if name not in names:
                errors.append(f"connector not registered: {name}")
                continue
            status = requests.get(
                f"{config.kafka_connect_url}/connectors/{name}/status", timeout=5
            ).json()
            if status.get("connector", {}).get("state") != "RUNNING":
                errors.append(f"connector not RUNNING: {name} -> {status}")
            failed_tasks = [
                task for task in status.get("tasks", []) if task.get("state") != "RUNNING"
            ]
            if failed_tasks:
                errors.append(f"connector task not RUNNING: {name} -> {failed_tasks}")
    except Exception as exc:  # noqa: BLE001
        errors.append(f"connector status check failed: {exc}")
    return errors


def _prefix_exists(client, bucket: str, prefix: str) -> bool:
    response = client.list_objects_v2(Bucket=bucket, Prefix=prefix, MaxKeys=1)
    return bool(response.get("Contents"))


def check_bronze(config: SmokeConfig) -> list[str]:
    client = s3_client(config)
    prefix = "bronze/kafka/cdc.behavior_events/"
    if not _prefix_exists(client, config.lake_bucket, prefix):
        return [f"missing bronze CDC objects: s3://{config.lake_bucket}/{prefix}"]
    return []


def check_offline_features(config: SmokeConfig) -> list[str]:
    client = s3_client(config)
    errors: list[str] = []
    for table in ["user_sequence_features", "user_aggregate_features", "item_features"]:
        prefix = f"offline/{table}/"
        if not _prefix_exists(client, config.feature_store_bucket, prefix):
            errors.append(f"missing offline feature objects: s3://{config.feature_store_bucket}/{prefix}")
    return errors


def check_redis_online(config: SmokeConfig) -> list[str]:
    client = redis.Redis(host=config.redis_host, port=config.redis_port, decode_responses=True)
    patterns = ["fs:user_sequence:*", "fs:user_aggregate:*", "fs:item:*", "candidate:*"]
    counts = {pattern: sum(1 for _ in client.scan_iter(pattern, count=100)) for pattern in patterns}
    if not any(counts.values()):
        return [f"missing Redis online feature keys: {counts}"]
    return []


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--phase",
        choices=["services", "buckets", "connectors", "bronze", "offline", "redis", "all"],
        default="all",
    )
    args = parser.parse_args()
    config = SmokeConfig()
    checks = []
    if args.phase in {"services", "all"}:
        checks.extend(check_services(config))
    if args.phase in {"buckets", "all"}:
        checks.extend(check_buckets(config))
    if args.phase in {"connectors", "all"}:
        checks.extend(check_connectors(config))
    if args.phase in {"bronze", "all"}:
        checks.extend(check_bronze(config))
    if args.phase in {"offline", "all"}:
        checks.extend(check_offline_features(config))
    if args.phase in {"redis", "all"}:
        checks.extend(check_redis_online(config))
    if checks:
        print({"passed": False, "errors": checks})
        return 1
    print({"passed": True, "phase": args.phase})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
