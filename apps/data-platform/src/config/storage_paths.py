from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class StorageBuckets:
    lake_bucket: str = "recsys-lakehouse"
    offline_feature_bucket: str = "recsys-offline-feature-store"
    lakehouse_warehouse: str = "s3a://recsys-lakehouse/warehouse"
    offline_feature_warehouse: str = "s3a://recsys-offline-feature-store/warehouse"


def lake_uri(path: str, buckets: StorageBuckets = StorageBuckets(), scheme: str = "s3a") -> str:
    clean = path.strip("/")
    return f"{scheme}://{buckets.lake_bucket}/{clean}"


def raw_uri(run_id: str, table_name: str, buckets: StorageBuckets = StorageBuckets()) -> str:
    return lake_uri(f"raw/{run_id}/{table_name}", buckets)


def lakehouse_warehouse_uri(buckets: StorageBuckets = StorageBuckets()) -> str:
    return buckets.lakehouse_warehouse


def silver_uri(table_name: str, buckets: StorageBuckets = StorageBuckets()) -> str:
    return lake_uri(f"silver/{table_name}", buckets)


def ml_artifact_uri(name: str, buckets: StorageBuckets = StorageBuckets()) -> str:
    return lake_uri(f"silver/ml/{name}", buckets)


def offline_feature_uri(feature_view_name: str, buckets: StorageBuckets = StorageBuckets()) -> str:
    return f"{buckets.offline_feature_warehouse.rstrip('/')}/feature_store/{feature_view_name}"


def assert_bucket_boundary(uri: str, buckets: StorageBuckets = StorageBuckets()) -> None:
    if uri.startswith(buckets.offline_feature_warehouse.rstrip("/") + "/feature_store/"):
        return
    if f"://{buckets.lake_bucket}/" in uri:
        return
    raise ValueError(f"Path violates recsys lakehouse boundary: {uri}")
