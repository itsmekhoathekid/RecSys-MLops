from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import pandas as pd


TRAINING_TABLE = "ml.bst_training_samples"
EVALUATION_TABLE = "ml.bst_evaluation_samples"
DEFAULT_CATALOG_NAME = "recsys_features"
DEFAULT_WAREHOUSE = "s3a://recsys-offline-feature-store/warehouse"

MODEL_SAMPLE_COLUMNS = [
    "impression_id",
    "request_id",
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

VERSIONED_SAMPLE_COLUMNS = [
    "sample_id",
    "entity_id",
    "impression_id",
    "request_id",
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
class HudiConfig:
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

    def table_name(self, table_ident: str) -> str:
        return table_ident.split(".")[-1]

    def table_path(self, table_ident: str) -> str:
        namespace = "/".join(table_ident.split(".")[:-1])
        return f"{self.warehouse.rstrip('/')}/{namespace}/{self.table_name(table_ident)}"


IcebergConfig = HudiConfig


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


def schema_hash_for(columns: list[str] = VERSIONED_SAMPLE_COLUMNS) -> str:
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
        return pd.DataFrame(columns=VERSIONED_SAMPLE_COLUMNS)
    return frame[VERSIONED_SAMPLE_COLUMNS]


def split_counts(splits: dict[str, list[dict[str, Any]]]) -> dict[str, int]:
    return {split: len(rows) for split, rows in splits.items()}


def _spark_session(config: HudiConfig):
    from pyspark.sql import SparkSession

    builder = (
        SparkSession.builder.appName("recsys-bst-dataset-versioning")
        .config("spark.serializer", "org.apache.spark.serializer.KryoSerializer")
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
            StructField("impression_id", StringType(), True),
            StructField("request_id", StringType(), True),
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


def _hudi_identifier_suffix(value: str) -> str:
    suffix = re.sub(r"[^A-Za-z0-9_]+", "_", value).strip("_")
    if not suffix:
        suffix = "run"
    if suffix[0].isdigit():
        suffix = f"run_{suffix}"
    return suffix


_iceberg_identifier_suffix = _hudi_identifier_suffix


def _hudi_options(table_name: str) -> dict[str, str]:
    return {
        "hoodie.table.name": table_name,
        "hoodie.datasource.write.table.name": table_name,
        "hoodie.datasource.write.table.type": "COPY_ON_WRITE",
        "hoodie.datasource.write.operation": "upsert",
        "hoodie.datasource.write.reconcile.schema": "true",
        "hoodie.datasource.write.recordkey.field": "sample_id",
        "hoodie.datasource.write.precombine.field": "updated_at",
        "hoodie.datasource.write.partitionpath.field": "split",
        "hoodie.datasource.write.hive_style_partitioning": "true",
        "hoodie.datasource.write.keygenerator.class": "org.apache.hudi.keygen.SimpleKeyGenerator",
    }


def _read_hudi_table(spark, table_path: str):
    return spark.read.format("hudi").load(table_path)


HUDI_CHANGE_IDENTITY_COLUMNS = ["sample_id", "row_hash", "split"]


def _filter_unchanged_hudi_rows(spark, incoming, table_path: str):
    """Return only new, content-changed, or split-moved records.

    A missing table is the initial load, so every incoming record must be
    written. Including ``split`` prevents an unchanged hash from hiding a
    partition-routing change.
    """
    try:
        existing = (
            _read_hudi_table(spark, table_path)
            .select(*HUDI_CHANGE_IDENTITY_COLUMNS)
            .dropDuplicates(HUDI_CHANGE_IDENTITY_COLUMNS)
        )
    except Exception:
        return incoming, False
    return incoming.join(existing, on=HUDI_CHANGE_IDENTITY_COLUMNS, how="left_anti"), True


def _latest_commit_time(spark, table_path: str) -> str | None:
    try:
        rows = _read_hudi_table(spark, table_path).selectExpr("max(_hoodie_commit_time) as commit_time").collect()
    except Exception:
        return None
    if not rows or rows[0]["commit_time"] is None:
        return None
    return str(rows[0]["commit_time"])


def _table_row_count(spark, table_path: str, splits: tuple[str, ...]) -> int:
    try:
        frame = _read_hudi_table(spark, table_path)
    except Exception:
        return 0
    rows = frame.where(frame["split"].isin(list(splits))).selectExpr("count(*) as row_count").collect()
    return int(rows[0]["row_count"])


def _write_jsonl_from_hudi(
    spark,
    table_path: str,
    split: str,
    output_path: Path,
) -> int:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        frame = _read_hudi_table(spark, table_path)
    except Exception:
        output_path.write_text("", encoding="utf-8")
        return 0
    rows = frame.where(frame["split"] == split).select(*MODEL_SAMPLE_COLUMNS).orderBy("event_timestamp", "sample_id").collect()
    with output_path.open("w", encoding="utf-8") as file:
        for row in rows:
            payload = {column: _json_normal(row[column]) for column in MODEL_SAMPLE_COLUMNS}
            file.write(json.dumps(payload, separators=(",", ":"), default=str) + "\n")
    return len(rows)


def commit_samples_to_hudi(
    samples: pd.DataFrame,
    output_dir: str | Path,
    dataset_run_id: str,
    config: HudiConfig,
) -> dict[str, Any]:
    ensure_warehouse_bucket(config.warehouse)
    spark = _spark_session(config)
    try:
        output = Path(output_dir)
        started = time.perf_counter()
        metadata: dict[str, Any] = {
            "enabled": True,
            "storage": "hudi",
            "catalog_name": config.catalog_name,
            "warehouse": config.warehouse,
            "tables": {},
            "latency_ms": {},
        }
        routes = {
            "training": (config.training_ident, ("train", "val"), f"bst_training_{_hudi_identifier_suffix(dataset_run_id)}"),
            "evaluation": (config.evaluation_ident, ("test",), f"bst_evaluation_{_hudi_identifier_suffix(dataset_run_id)}"),
        }
        for key, (table_ident, split_values, tag_name) in routes.items():
            route_started = time.perf_counter()
            subset = samples[samples["split"].isin(split_values)]
            table_path = config.table_path(table_ident)
            table_name = config.table_name(table_ident)
            input_rows = len(subset)
            changed_rows = input_rows
            skipped_unchanged_rows = 0
            write_performed = False
            if not subset.empty:
                incoming = spark.createDataFrame(_spark_safe_records(subset), schema=_sample_schema())
                changes, compared_with_snapshot = _filter_unchanged_hudi_rows(spark, incoming, table_path)
                if compared_with_snapshot:
                    changes = changes.persist()
                    changed_rows = changes.count()
                    skipped_unchanged_rows = input_rows - changed_rows
                try:
                    if changed_rows > 0:
                        (
                            changes.write.format("hudi")
                            .options(**_hudi_options(table_name))
                            .mode("append")
                            .save(table_path)
                        )
                        write_performed = True
                finally:
                    if compared_with_snapshot:
                        changes.unpersist()
            commit_time = _latest_commit_time(spark, table_path)
            commit_latency_ms = round((time.perf_counter() - route_started) * 1000, 3)
            metadata["latency_ms"][f"{key}_commit"] = commit_latency_ms
            metadata["tables"][key] = {
                "name": table_ident,
                "path": table_path,
                "snapshot_id": commit_time,
                "commit_time": commit_time,
                "tag": tag_name,
                "row_count": _table_row_count(spark, table_path, split_values),
                "input_rows": input_rows,
                "changed_rows": changed_rows,
                "skipped_unchanged_rows": skipped_unchanged_rows,
                "write_performed": write_performed,
                "splits": list(split_values),
                "latency_ms": commit_latency_ms,
            }

        jsonl_started = time.perf_counter()
        jsonl_counts = {
            "train": _write_jsonl_from_hudi(spark, config.table_path(config.training_ident), "train", output / "train.jsonl"),
            "val": _write_jsonl_from_hudi(spark, config.table_path(config.training_ident), "val", output / "val.jsonl"),
            "test": _write_jsonl_from_hudi(spark, config.table_path(config.evaluation_ident), "test", output / "test.jsonl"),
        }
        metadata["latency_ms"]["jsonl_export"] = round((time.perf_counter() - jsonl_started) * 1000, 3)
        metadata["latency_ms"]["total"] = round((time.perf_counter() - started) * 1000, 3)
        metadata["jsonl_counts"] = jsonl_counts
        return metadata
    finally:
        spark.stop()


commit_samples_to_iceberg = commit_samples_to_hudi


def local_dataset_version_metadata(
    output_dir: str | Path,
    splits: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    output = Path(output_dir)
    return {
        "enabled": False,
        "storage": "local",
        "latency_ms": {},
        "tables": {
            "training": {
                "name": f"{DEFAULT_CATALOG_NAME}.{TRAINING_TABLE}",
                "snapshot_id": None,
                "commit_time": None,
                "tag": "",
                "row_count": len(splits.get("train", [])) + len(splits.get("val", [])),
                "splits": ["train", "val"],
            },
            "evaluation": {
                "name": f"{DEFAULT_CATALOG_NAME}.{EVALUATION_TABLE}",
                "snapshot_id": None,
                "commit_time": None,
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
