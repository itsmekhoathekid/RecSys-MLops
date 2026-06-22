from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class StorageBuckets:
    lake_bucket: str = "recsys-lake"
    feature_store_bucket: str = "recsys-feature-store"


def lake_uri(path: str, buckets: StorageBuckets = StorageBuckets(), scheme: str = "s3a") -> str:
    clean = path.strip("/")
    return f"{scheme}://{buckets.lake_bucket}/{clean}"


def feature_store_uri(path: str, buckets: StorageBuckets = StorageBuckets(), scheme: str = "s3a") -> str:
    clean = path.strip("/")
    return f"{scheme}://{buckets.feature_store_bucket}/{clean}"


def raw_uri(run_id: str, table_name: str, buckets: StorageBuckets = StorageBuckets()) -> str:
    return lake_uri(f"raw/{run_id}/{table_name}", buckets)


def bronze_kafka_uri(topic: str, buckets: StorageBuckets = StorageBuckets()) -> str:
    return lake_uri(f"bronze/kafka/{topic}", buckets)


def silver_uri(table_name: str, buckets: StorageBuckets = StorageBuckets()) -> str:
    return lake_uri(f"silver/{table_name}", buckets)


def ml_artifact_uri(name: str, buckets: StorageBuckets = StorageBuckets()) -> str:
    return lake_uri(f"silver/ml/{name}", buckets)


def offline_feature_uri(feature_view_name: str, buckets: StorageBuckets = StorageBuckets()) -> str:
    return feature_store_uri(f"offline/{feature_view_name}", buckets)


def assert_bucket_boundary(uri: str, buckets: StorageBuckets = StorageBuckets()) -> None:
    if f"://{buckets.feature_store_bucket}/offline/" in uri:
        return
    if f"://{buckets.lake_bucket}/" in uri and "/offline/" not in uri:
        return
    raise ValueError(f"Path violates recsys lake/feature-store boundary: {uri}")

