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
    raw_path = str(path)
    if raw_path.startswith("s3://"):
        target = raw_path.rstrip("/")
        if target.endswith(".parquet"):
            files = [target]
        else:
            import s3fs

            fs = s3fs.S3FileSystem(
                key=os.getenv("MINIO_ROOT_USER", os.getenv("AWS_ACCESS_KEY_ID", "minio")),
                secret=os.getenv("MINIO_ROOT_PASSWORD", os.getenv("AWS_SECRET_ACCESS_KEY", "minio123")),
                client_kwargs={
                    "endpoint_url": os.getenv("MINIO_ENDPOINT", "http://minio:9000")
                },
            )
            files = sorted(fs.glob(f"{target}/*.parquet"))
            files = [f"s3://{file}" if not file.startswith("s3://") else file for file in files]
        if not files:
            raise FileNotFoundError(f"No feature parquet files under {raw_path}")
        return pd.concat(
            [
                pd.read_parquet(
                    file,
                    storage_options={
                        "client_kwargs": {
                            "endpoint_url": os.getenv("MINIO_ENDPOINT", "http://minio:9000")
                        },
                        "key": os.getenv("MINIO_ROOT_USER", os.getenv("AWS_ACCESS_KEY_ID", "minio")),
                        "secret": os.getenv(
                            "MINIO_ROOT_PASSWORD",
                            os.getenv("AWS_SECRET_ACCESS_KEY", "minio123"),
                        ),
                    },
                )
                for file in files
            ],
            ignore_index=True,
        )
    root = Path(path)
    files = sorted(root.rglob("*.parquet")) if root.is_dir() else [root]
    if not files:
        raise FileNotFoundError(f"No feature parquet files under {root}")
    return pd.concat([pd.read_parquet(file) for file in files], ignore_index=True)
