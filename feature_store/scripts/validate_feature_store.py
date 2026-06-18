from __future__ import annotations

from pathlib import Path
import os


FEATURE_VIEWS = [
    "user_sequence_features",
    "user_aggregate_features",
    "item_features",
]


def validate_s3_feature_store(root: str) -> tuple[bool, list[str]]:
    import boto3

    endpoint = os.getenv("MINIO_ENDPOINT", "http://minio:9000")
    bucket = root.replace("s3://", "").split("/", 1)[0]
    prefix = root.replace(f"s3://{bucket}/", "").strip("/")
    client = boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=os.getenv("MINIO_ROOT_USER", "minio"),
        aws_secret_access_key=os.getenv("MINIO_ROOT_PASSWORD", "minio123"),
        region_name="us-east-1",
    )
    missing = []
    for feature_view in FEATURE_VIEWS:
        response = client.list_objects_v2(
            Bucket=bucket,
            Prefix=f"{prefix}/{feature_view}/",
            MaxKeys=1,
        )
        if response.get("KeyCount", 0) == 0:
            missing.append(f"s3://{bucket}/{prefix}/{feature_view}")
    return not missing, missing


def main() -> int:
    if not Path("feature_store/feature_repo/feature_store.yaml").exists():
        print({"passed": False, "missing": ["feature_store/feature_repo/feature_store.yaml"]})
        return 1

    offline_root = os.getenv("FEAST_OFFLINE_ROOT", "data_pipeline/output/feature_store/offline")
    if offline_root.startswith("s3://"):
        passed, missing = validate_s3_feature_store(offline_root)
        print({"passed": passed, "missing": missing})
        return 0 if passed else 1

    required = [Path(offline_root) / name for name in FEATURE_VIEWS]
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        print({"passed": False, "missing": missing})
        return 1
    print({"passed": True, "missing": []})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
