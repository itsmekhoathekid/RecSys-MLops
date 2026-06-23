from __future__ import annotations

import json
import os

import boto3


def main() -> int:
    endpoint = os.getenv("MINIO_ENDPOINT", "http://data-platform-minio:9000")
    user = os.getenv("MINIO_ROOT_USER", os.getenv("AWS_ACCESS_KEY_ID", "minio"))
    password = os.getenv("MINIO_ROOT_PASSWORD", os.getenv("AWS_SECRET_ACCESS_KEY", "minio123"))
    buckets = [
        os.getenv("LAKE_BUCKET", "recsys-lake"),
        os.getenv("FEATURE_STORE_BUCKET", "recsys-feature-store"),
    ]
    client = boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=user,
        aws_secret_access_key=password,
        region_name=os.getenv("AWS_DEFAULT_REGION", "us-east-1"),
    )
    existing = {bucket["Name"] for bucket in client.list_buckets()["Buckets"]}
    created = []
    for bucket in buckets:
        if bucket in existing:
            continue
        client.create_bucket(Bucket=bucket)
        created.append(bucket)
    print(json.dumps({"endpoint": endpoint, "buckets": buckets, "created": created}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

