from __future__ import annotations

from pathlib import Path
import os

import pandas as pd


def write_feature_table(frame: pd.DataFrame, output_path: str | Path) -> Path:
    raw_path = str(output_path)
    if raw_path.startswith("s3://"):
        target = raw_path.rstrip("/") + "/part-00000.parquet"
        frame.to_parquet(
            target,
            index=False,
            storage_options={
                "client_kwargs": {
                    "endpoint_url": os.getenv("MINIO_ENDPOINT", "http://minio:9000")
                },
                "key": os.getenv("MINIO_ROOT_USER", "minio"),
                "secret": os.getenv("MINIO_ROOT_PASSWORD", "minio123"),
            },
        )
        return Path(target)
    path = Path(output_path)
    path.mkdir(parents=True, exist_ok=True)
    output = path / "part-00000.parquet"
    frame.to_parquet(output, index=False)
    return output


def read_feature_table(path: str | Path) -> pd.DataFrame:
    root = Path(path)
    files = sorted(root.rglob("*.parquet")) if root.is_dir() else [root]
    if not files:
        raise FileNotFoundError(f"No feature parquet files under {root}")
    return pd.concat([pd.read_parquet(file) for file in files], ignore_index=True)
