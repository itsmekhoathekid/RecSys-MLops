from __future__ import annotations

import hashlib
import json
import os
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import pandas as pd


TRAINING_TABLE = "ml.bst_training_samples"
EVALUATION_TABLE = "ml.bst_evaluation_samples"
DEFAULT_CATALOG_NAME = "recsys"
DEFAULT_WAREHOUSE = "s3a://recsys-lake/silver/ml/iceberg"

MODEL_SAMPLE_COLUMNS = [
    "user_id",
    "hist_item_id",
    "hist_event_type",
    "hist_category",
    "hist_brand",
    "hist_price_bucket",
    "hist_time",
    "target_item_id",
    "target_category",
    "target_brand",
    "target_price_bucket",
    "event_time",
    "label",
]

SEQUENCE_SAMPLE_COLUMNS = [
    "hist_item_id",
    "hist_event_type",
    "hist_category",
    "hist_brand",
    "hist_price_bucket",
    "hist_time",
]

ICEBERG_COLUMNS = [
    "sample_id",
    "entity_id",
    "user_id",
    "target_item_id",
    "event_timestamp",
    "split",
    "label",
    "hist_item_id",
    "hist_event_type",
    "hist_category",
    "hist_brand",
    "hist_price_bucket",
    "hist_time",
    "target_category",
    "target_brand",
    "target_price_bucket",
    "event_time",
    "features_json",
    "feature_service_version",
    "processing_code_version",
    "row_hash",
    "dataset_run_id",
    "created_at",
    "updated_at",
]


@dataclass(frozen=True)
class IcebergConfig:
    catalog_name: str = DEFAULT_CATALOG_NAME
    warehouse: str = DEFAULT_WAREHOUSE
    training_table: str = TRAINING_TABLE
    evaluation_table: str = EVALUATION_TABLE

    @property
    def training_ident(self) -> str:
        return f"{self.catalog_name}.{self.training_table}"

    @property
    def evaluation_ident(self) -> str:
        return f"{self.catalog_name}.{self.evaluation_table}"


def timestamp_run_id(now: datetime | None = None) -> str:
    stamp = now or datetime.now(timezone.utc)
    return stamp.strftime("%Y%m%dT%H%M%SZ")


def processing_code_version(repo_root: str | Path = ".") -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=repo_root,
            check=True,
            text=True,
            capture_output=True,
        )
    except Exception:
        return "unknown"
    return result.stdout.strip() or "unknown"


def _json_normal(value: Any) -> Any:
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _json_normal(item) for key, item in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [_json_normal(item) for item in value]
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    return value


def stable_json(payload: dict[str, Any]) -> str:
    return json.dumps(_json_normal(payload), sort_keys=True, separators=(",", ":"), default=str)


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sample_id_for(row: dict[str, Any]) -> str:
    event_time = row.get("event_time", "")
    key = "|".join(
        [
            str(row.get("impression_id", "")),
            str(row.get("request_id", "")),
            str(row.get("user_id", "")),
            str(row.get("target_item_id", "")),
            str(event_time),
        ]
    )
    return sha256_text(key)


def row_hash_for(row: dict[str, Any]) -> str:
    payload = {column: row.get(column) for column in MODEL_SAMPLE_COLUMNS}
    payload["prediction_timestamp"] = row.get("prediction_timestamp")
    return sha256_text(stable_json(payload))


def schema_hash_for(columns: list[str] = ICEBERG_COLUMNS) -> str:
    return sha256_text(stable_json({"columns": columns}))


def to_versioned_samples(
    splits: dict[str, list[dict[str, Any]]],
    dataset_run_id: str,
    feature_service_version: str,
    processing_code: str,
) -> pd.DataFrame:
    now = datetime.now(timezone.utc)
    records: list[dict[str, Any]] = []
    for split, rows in splits.items():
        for row in rows:
            normalized = dict(row)
            event_time = int(normalized.get("event_time", 0))
            event_timestamp = datetime.fromtimestamp(event_time, tz=timezone.utc)
            features = {
                column: normalized.get(column)
                for column in MODEL_SAMPLE_COLUMNS
                if column not in {"label"}
            }
            record = {
                "sample_id": sample_id_for(normalized),
                "entity_id": str(normalized.get("user_id", "")),
                "user_id": int(normalized.get("user_id", 0)),
                "target_item_id": int(normalized.get("target_item_id", 0)),
                "event_timestamp": event_timestamp,
                "split": split,
                "label": int(normalized.get("label", 0)),
                "features_json": stable_json(features),
                "feature_service_version": feature_service_version,
                "processing_code_version": processing_code,
                "row_hash": row_hash_for(normalized),
                "dataset_run_id": dataset_run_id,
                "created_at": now,
                "updated_at": now,
            }
            for column in MODEL_SAMPLE_COLUMNS:
                if column not in record:
                    record[column] = normalized.get(column)
            records.append(record)
    frame = pd.DataFrame(records)
    if frame.empty:
        return pd.DataFrame(columns=ICEBERG_COLUMNS)
    return frame[ICEBERG_COLUMNS]


def split_counts(splits: dict[str, list[dict[str, Any]]]) -> dict[str, int]:
    return {split: len(rows) for split, rows in splits.items()}


def _spark_session(config: IcebergConfig):
    from pyspark.sql import SparkSession

    builder = (
        SparkSession.builder.appName("recsys-bst-dataset-versioning")
        .config("spark.sql.extensions", "org.apache.iceberg.spark.extensions.IcebergSparkSessionExtensions")
        .config(f"spark.sql.catalog.{config.catalog_name}", "org.apache.iceberg.spark.SparkCatalog")
        .config(f"spark.sql.catalog.{config.catalog_name}.type", "hadoop")
        .config(f"spark.sql.catalog.{config.catalog_name}.warehouse", config.warehouse)
        .config("spark.hadoop.fs.s3a.endpoint", os.getenv("MINIO_ENDPOINT", "http://minio:9000"))
        .config("spark.hadoop.fs.s3a.access.key", os.getenv("AWS_ACCESS_KEY_ID", os.getenv("MINIO_ROOT_USER", "minio")))
        .config(
            "spark.hadoop.fs.s3a.secret.key",
            os.getenv("AWS_SECRET_ACCESS_KEY", os.getenv("MINIO_ROOT_PASSWORD", "minio123")),
        )
        .config("spark.hadoop.fs.s3a.path.style.access", "true")
        .config("spark.hadoop.fs.s3a.connection.ssl.enabled", "false")
    )
    return builder.getOrCreate()


def ensure_warehouse_bucket(warehouse: str) -> None:
    parsed = urlparse(warehouse)
    if parsed.scheme not in {"s3", "s3a"} or not parsed.netloc:
        return
    import boto3
    from botocore.exceptions import ClientError

    client = boto3.client(
        "s3",
        endpoint_url=os.getenv("MINIO_ENDPOINT", "http://minio:9000"),
        aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID", os.getenv("MINIO_ROOT_USER", "minio")),
        aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY", os.getenv("MINIO_ROOT_PASSWORD", "minio123")),
        region_name=os.getenv("AWS_DEFAULT_REGION", "us-east-1"),
    )
    try:
        client.head_bucket(Bucket=parsed.netloc)
    except ClientError as exc:
        code = str(exc.response.get("Error", {}).get("Code", ""))
        if code not in {"404", "NoSuchBucket", "NotFound"}:
            raise
        client.create_bucket(Bucket=parsed.netloc)


def _sample_schema():
    from pyspark.sql.types import (
        ArrayType,
        LongType,
        StringType,
        StructField,
        StructType,
        TimestampType,
    )

    return StructType(
        [
            StructField("sample_id", StringType(), False),
            StructField("entity_id", StringType(), True),
            StructField("user_id", LongType(), True),
            StructField("target_item_id", LongType(), True),
            StructField("event_timestamp", TimestampType(), True),
            StructField("split", StringType(), True),
            StructField("label", LongType(), True),
            StructField("hist_item_id", ArrayType(LongType()), True),
            StructField("hist_event_type", ArrayType(LongType()), True),
            StructField("hist_category", ArrayType(LongType()), True),
            StructField("hist_brand", ArrayType(LongType()), True),
            StructField("hist_price_bucket", ArrayType(LongType()), True),
            StructField("hist_time", ArrayType(LongType()), True),
            StructField("target_category", LongType(), True),
            StructField("target_brand", LongType(), True),
            StructField("target_price_bucket", LongType(), True),
            StructField("event_time", LongType(), True),
            StructField("features_json", StringType(), True),
            StructField("feature_service_version", StringType(), True),
            StructField("processing_code_version", StringType(), True),
            StructField("row_hash", StringType(), True),
            StructField("dataset_run_id", StringType(), True),
            StructField("created_at", TimestampType(), True),
            StructField("updated_at", TimestampType(), True),
        ]
    )


def _spark_timestamp(value: Any) -> datetime | None:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is not None:
        timestamp = timestamp.tz_convert("UTC").tz_localize(None)
    return timestamp.to_pydatetime()


def _spark_safe_records(frame: pd.DataFrame) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for raw_record in frame.to_dict(orient="records"):
        record = dict(raw_record)
        for column in ("event_timestamp", "created_at", "updated_at"):
            record[column] = _spark_timestamp(record.get(column))
        for column in SEQUENCE_SAMPLE_COLUMNS:
            values = record.get(column) or []
            record[column] = [int(value) for value in values]
        for column in ("user_id", "target_item_id", "label", "target_category", "target_brand", "target_price_bucket", "event_time"):
            value = record.get(column)
            if value is not None:
                record[column] = int(value)
        records.append(record)
    return records


def _array_type(column: str) -> str:
    return "ARRAY<BIGINT>" if column in SEQUENCE_SAMPLE_COLUMNS else "BIGINT"


def _create_table_sql(table_ident: str) -> str:
    model_columns = ",\n  ".join(
        f"{column} {_array_type(column)}"
        for column in [
            "hist_item_id",
            "hist_event_type",
            "hist_category",
            "hist_brand",
            "hist_price_bucket",
            "hist_time",
        ]
    )
    return f"""
CREATE TABLE IF NOT EXISTS {table_ident} (
  sample_id STRING,
  entity_id STRING,
  user_id BIGINT,
  target_item_id BIGINT,
  event_timestamp TIMESTAMP,
  split STRING,
  label BIGINT,
  {model_columns},
  target_category BIGINT,
  target_brand BIGINT,
  target_price_bucket BIGINT,
  event_time BIGINT,
  features_json STRING,
  feature_service_version STRING,
  processing_code_version STRING,
  row_hash STRING,
  dataset_run_id STRING,
  created_at TIMESTAMP,
  updated_at TIMESTAMP
) USING iceberg
PARTITIONED BY (days(event_timestamp), split)
"""


def _merge_sql(table_ident: str, view_name: str) -> str:
    update_columns = [column for column in ICEBERG_COLUMNS if column != "created_at"]
    update_sql = ", ".join(f"{column} = s.{column}" for column in update_columns)
    insert_columns = ", ".join(ICEBERG_COLUMNS)
    insert_values = ", ".join(f"s.{column}" for column in ICEBERG_COLUMNS)
    return f"""
MERGE INTO {table_ident} t
USING {view_name} s
ON t.sample_id = s.sample_id
WHEN MATCHED AND (t.row_hash <> s.row_hash OR t.split <> s.split) THEN UPDATE SET {update_sql}
WHEN NOT MATCHED THEN INSERT ({insert_columns}) VALUES ({insert_values})
"""


def _latest_snapshot_id(spark, table_ident: str) -> int | None:
    rows = spark.sql(f"SELECT snapshot_id FROM {table_ident}.snapshots ORDER BY committed_at DESC LIMIT 1").collect()
    if not rows:
        return None
    return int(rows[0]["snapshot_id"])


def _create_tag(spark, table_ident: str, tag_name: str, snapshot_id: int | None) -> None:
    if snapshot_id is None:
        return
    try:
        spark.sql(f"ALTER TABLE {table_ident} CREATE TAG {tag_name} AS OF VERSION {snapshot_id}")
    except Exception as exc:
        if "already exists" not in str(exc).lower():
            raise


def _table_row_count(spark, table_ident: str, snapshot_id: int | None, splits: tuple[str, ...]) -> int:
    if snapshot_id is None:
        return 0
    split_sql = ", ".join(f"'{split}'" for split in splits)
    rows = spark.sql(
        f"SELECT COUNT(*) AS row_count FROM {table_ident} VERSION AS OF {snapshot_id} WHERE split IN ({split_sql})"
    ).collect()
    return int(rows[0]["row_count"])


def _write_jsonl_from_snapshot(
    spark,
    table_ident: str,
    snapshot_id: int | None,
    split: str,
    output_path: Path,
) -> int:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if snapshot_id is None:
        output_path.write_text("", encoding="utf-8")
        return 0
    columns = ", ".join(MODEL_SAMPLE_COLUMNS)
    rows = spark.sql(
        f"""
        SELECT {columns}
        FROM {table_ident} VERSION AS OF {snapshot_id}
        WHERE split = '{split}'
        ORDER BY event_timestamp, sample_id
        """
    ).collect()
    with output_path.open("w", encoding="utf-8") as file:
        for row in rows:
            payload = {column: _json_normal(row[column]) for column in MODEL_SAMPLE_COLUMNS}
            file.write(json.dumps(payload, separators=(",", ":"), default=str) + "\n")
    return len(rows)


def commit_samples_to_iceberg(
    samples: pd.DataFrame,
    output_dir: str | Path,
    dataset_run_id: str,
    config: IcebergConfig,
) -> dict[str, Any]:
    ensure_warehouse_bucket(config.warehouse)
    spark = _spark_session(config)
    spark.sql(f"CREATE NAMESPACE IF NOT EXISTS {config.catalog_name}.ml")
    spark.sql(_create_table_sql(config.training_ident))
    spark.sql(_create_table_sql(config.evaluation_ident))

    output = Path(output_dir)
    metadata: dict[str, Any] = {
        "enabled": True,
        "catalog_name": config.catalog_name,
        "warehouse": config.warehouse,
        "tables": {},
    }
    routes = {
        "training": (config.training_ident, ("train", "val"), f"bst_training_{dataset_run_id}"),
        "evaluation": (config.evaluation_ident, ("test",), f"bst_evaluation_{dataset_run_id}"),
    }
    for key, (table_ident, split_values, tag_name) in routes.items():
        subset = samples[samples["split"].isin(split_values)]
        if not subset.empty:
            view_name = f"staged_{key}_{dataset_run_id}".replace("-", "_")
            spark.createDataFrame(_spark_safe_records(subset), schema=_sample_schema()).createOrReplaceTempView(view_name)
            spark.sql(_merge_sql(table_ident, view_name))
        snapshot_id = _latest_snapshot_id(spark, table_ident)
        _create_tag(spark, table_ident, tag_name, snapshot_id)
        metadata["tables"][key] = {
            "name": table_ident,
            "snapshot_id": snapshot_id,
            "tag": tag_name,
            "row_count": _table_row_count(spark, table_ident, snapshot_id, split_values),
            "splits": list(split_values),
        }

    jsonl_counts = {
        "train": _write_jsonl_from_snapshot(spark, config.training_ident, metadata["tables"]["training"]["snapshot_id"], "train", output / "train.jsonl"),
        "val": _write_jsonl_from_snapshot(spark, config.training_ident, metadata["tables"]["training"]["snapshot_id"], "val", output / "val.jsonl"),
        "test": _write_jsonl_from_snapshot(spark, config.evaluation_ident, metadata["tables"]["evaluation"]["snapshot_id"], "test", output / "test.jsonl"),
    }
    metadata["jsonl_counts"] = jsonl_counts
    return metadata


def local_dataset_version_metadata(
    output_dir: str | Path,
    splits: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    output = Path(output_dir)
    return {
        "enabled": False,
        "tables": {
            "training": {
                "name": f"{DEFAULT_CATALOG_NAME}.{TRAINING_TABLE}",
                "snapshot_id": None,
                "tag": "",
                "row_count": len(splits.get("train", [])) + len(splits.get("val", [])),
                "splits": ["train", "val"],
            },
            "evaluation": {
                "name": f"{DEFAULT_CATALOG_NAME}.{EVALUATION_TABLE}",
                "snapshot_id": None,
                "tag": "",
                "row_count": len(splits.get("test", [])),
                "splits": ["test"],
            },
        },
        "jsonl_counts": split_counts(splits),
        "jsonl_paths": {
            split: str(output / f"{split}.jsonl")
            for split in ("train", "val", "test")
        },
    }
