from __future__ import annotations

import json
import os
from dataclasses import dataclass

import boto3
import redis

from warehouse.connection import connect


@dataclass(frozen=True)
class Check:
    name: str
    passed: bool
    details: dict


def s3_client():
    return boto3.client(
        "s3",
        endpoint_url=os.getenv("MINIO_ENDPOINT", "http://data-platform-minio:9000"),
        aws_access_key_id=os.getenv("MINIO_ROOT_USER", os.getenv("AWS_ACCESS_KEY_ID", "minio")),
        aws_secret_access_key=os.getenv("MINIO_ROOT_PASSWORD", os.getenv("AWS_SECRET_ACCESS_KEY", "minio123")),
        region_name=os.getenv("AWS_DEFAULT_REGION", "us-east-1"),
    )


def prefix_count(bucket: str, prefix: str) -> int:
    response = s3_client().list_objects_v2(Bucket=bucket, Prefix=prefix, MaxKeys=1000)
    return len(response.get("Contents", []))


def table_count(qualified_name: str) -> int:
    with connect() as connection:
        with connection.cursor() as cursor:
            cursor.execute(f"SELECT COUNT(*) FROM {qualified_name}")
            return int(cursor.fetchone()[0])


def redis_count(pattern: str) -> int:
    client = redis.Redis(
        host=os.getenv("REDIS_HOST", "redis"),
        port=int(os.getenv("REDIS_PORT", "6379")),
        decode_responses=True,
    )
    return sum(1 for _ in client.scan_iter(pattern, count=100))


def main() -> int:
    lake_bucket = os.getenv("LAKE_BUCKET", "recsys-lake")
    feature_bucket = os.getenv("FEATURE_STORE_BUCKET", "recsys-feature-store")
    checks = [
        Check("raw_lake", prefix_count(lake_bucket, "raw/test_10k_seed42/") > 0, {"prefix": f"s3://{lake_bucket}/raw/test_10k_seed42/"}),
        Check("bronze_cdc", prefix_count(lake_bucket, "bronze/kafka/cdc.behavior_events/") > 0, {"prefix": f"s3://{lake_bucket}/bronze/kafka/cdc.behavior_events/"}),
        Check("ge_report", prefix_count(lake_bucket, "monitoring/great_expectations/staging_validation.json") > 0, {"prefix": f"s3://{lake_bucket}/monitoring/great_expectations/staging_validation.json"}),
        Check("offline_user_sequence", prefix_count(feature_bucket, "offline/user_sequence_features/") > 0, {"prefix": f"s3://{feature_bucket}/offline/user_sequence_features/"}),
        Check("offline_user_aggregate", prefix_count(feature_bucket, "offline/user_aggregate_features/") > 0, {"prefix": f"s3://{feature_bucket}/offline/user_aggregate_features/"}),
        Check("offline_item_features", prefix_count(feature_bucket, "offline/item_features/") > 0, {"prefix": f"s3://{feature_bucket}/offline/item_features/"}),
        Check("staging_stream_behavior_events", table_count("staging.stream_behavior_events") > 0, {"table": "staging.stream_behavior_events"}),
        Check("production_fact_behavior_events", table_count("production.fact_behavior_events") > 0, {"table": "production.fact_behavior_events"}),
        Check("online_store_sync_runs", table_count("monitoring.online_store_sync_runs") > 0, {"table": "monitoring.online_store_sync_runs"}),
        Check("redis_user_sequence", redis_count("fs:user_sequence:*") > 0, {"pattern": "fs:user_sequence:*"}),
        Check("redis_user_aggregate", redis_count("fs:user_aggregate:*") > 0, {"pattern": "fs:user_aggregate:*"}),
        Check("redis_item_features", redis_count("fs:item:*") > 0, {"pattern": "fs:item:*"}),
    ]
    payload = {"passed": all(check.passed for check in checks), "checks": [check.__dict__ for check in checks]}
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if payload["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

