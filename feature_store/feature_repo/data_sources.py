from __future__ import annotations

import os

try:
    from feast import FileSource
except ImportError:  # pragma: no cover
    FileSource = None


OFFLINE_ROOT = os.getenv(
    "FEAST_OFFLINE_ROOT",
    "s3://recsys-feature-store/offline",
)


def _file_source(name: str):
    if FileSource is None:
        return None
    return FileSource(
        path=f"{OFFLINE_ROOT.rstrip('/')}/{name}",
        timestamp_field="event_timestamp",
        created_timestamp_column="created_timestamp",
        s3_endpoint_override=os.getenv("FEAST_S3_ENDPOINT", os.getenv("MINIO_ENDPOINT", "http://minio:9000")),
    )


user_sequence_source = _file_source("user_sequence_features")
user_aggregate_source = _file_source("user_aggregate_features")
item_features_source = _file_source("item_features")
