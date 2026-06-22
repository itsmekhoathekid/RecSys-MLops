from __future__ import annotations

from pathlib import Path
import os
from urllib.parse import urlparse

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq


TABLES = (
    "users",
    "user_preferences",
    "products",
    "product_snapshots",
    "sessions",
    "recommendation_requests",
    "impressions",
    "behavior_events",
    "orders",
    "order_items",
)


def resolve_local_path(path: str | Path) -> Path:
    """Resolve local paths and fail clearly for remote S3 paths in local mode."""
    raw = str(path)
    parsed = urlparse(raw)
    if parsed.scheme == "s3":
        raise ValueError(
            "S3/MinIO paths require Spark or fsspec runtime. "
            f"Use a mounted/local path for local POC reads: {raw}"
        )
    return Path(raw)


def s3_storage_options() -> dict:
    return {
        "key": os.getenv("MINIO_ROOT_USER", "minio"),
        "secret": os.getenv("MINIO_ROOT_PASSWORD", "minio123"),
        "client_kwargs": {
            "endpoint_url": os.getenv("MINIO_ENDPOINT", "http://minio:9000"),
            "region_name": "us-east-1",
        },
    }


def read_s3_parquet_dataset(path: str) -> pd.DataFrame:
    import s3fs

    parsed = urlparse(path)
    root = f"{parsed.netloc}{parsed.path}".rstrip("/")
    filesystem = s3fs.S3FileSystem(anon=False, **s3_storage_options())
    files = sorted(file for file in filesystem.find(root) if file.endswith(".parquet"))
    if not files:
        raise FileNotFoundError(f"No parquet files found under {path}")
    tables = []
    for file in files:
        with filesystem.open(file, "rb") as handle:
            tables.append(pq.ParquetFile(handle).read())
    return pa.concat_tables(tables).to_pandas()


def read_parquet_dataset(path: str | Path) -> pd.DataFrame:
    raw = str(path)
    if raw.startswith("s3://"):
        return read_s3_parquet_dataset(raw)
    root = resolve_local_path(path)
    files = sorted(root.rglob("*.parquet")) if root.is_dir() else [root]
    if not files or not all(file.exists() for file in files):
        raise FileNotFoundError(f"No parquet files found under {root}")
    table = pa.concat_tables([pq.ParquetFile(file).read() for file in files])
    return table.to_pandas()


def read_generator_table(run_path: str | Path, table_name: str) -> pd.DataFrame:
    if table_name not in TABLES:
        raise ValueError(f"Unsupported generator table: {table_name}")
    raw = str(run_path).rstrip("/")
    if raw.startswith("s3://"):
        return read_parquet_dataset(f"{raw}/{table_name}")
    return read_parquet_dataset(resolve_local_path(run_path) / table_name)


def read_generator_run(run_path: str | Path) -> dict[str, pd.DataFrame]:
    return {table: read_generator_table(run_path, table) for table in TABLES}
