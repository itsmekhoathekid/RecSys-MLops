from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import pandas as pd


DATASET_TABLE = "ml.bst_samples_native_v2"
DEFAULT_CATALOG_NAME = "recsys_features"
DEFAULT_WAREHOUSE = "s3a://recsys-offline-feature-store/warehouse"
DEFAULT_CLEAN_HOURS_RETAINED = 2160
DEFAULT_ZK_URL = "zookeeper.recsys-dataflow.svc.cluster.local"
DEFAULT_ZK_PORT = 2181
DEFAULT_ZK_BASE_PATH = "/hudi/locks/bst_samples_native_v2"
DEFAULT_ZK_LOCK_KEY = "bst_samples_native_v2"

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

HUDI_RECORD_KEY_COLUMNS = ["impression_id", "target_item_id"]
HUDI_SAMPLE_COLUMNS = [
    *MODEL_SAMPLE_COLUMNS,
    "event_timestamp",
    "split",
    "source_updated_at",
    "_hoodie_is_deleted",
]


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class HudiConfig:
    catalog_name: str = DEFAULT_CATALOG_NAME
    warehouse: str = DEFAULT_WAREHOUSE
    dataset_table: str = DATASET_TABLE
    clean_hours_retained: int = DEFAULT_CLEAN_HOURS_RETAINED
    occ_enabled: bool = True
    zookeeper_url: str = DEFAULT_ZK_URL
    zookeeper_port: int = DEFAULT_ZK_PORT
    zookeeper_base_path: str = DEFAULT_ZK_BASE_PATH
    zookeeper_lock_key: str = DEFAULT_ZK_LOCK_KEY

    @classmethod
    def from_env(
        cls,
        *,
        catalog_name: str = DEFAULT_CATALOG_NAME,
        warehouse: str = DEFAULT_WAREHOUSE,
        dataset_table: str | None = None,
    ) -> HudiConfig:
        return cls(
            catalog_name=catalog_name,
            warehouse=warehouse,
            dataset_table=dataset_table or os.getenv("HUDI_DATASET_TABLE", DATASET_TABLE),
            clean_hours_retained=int(
                os.getenv("HUDI_CLEAN_HOURS_RETAINED", str(DEFAULT_CLEAN_HOURS_RETAINED))
            ),
            occ_enabled=_env_bool("HUDI_OCC_ENABLED", True),
            zookeeper_url=os.getenv("HUDI_ZK_URL", DEFAULT_ZK_URL),
            zookeeper_port=int(os.getenv("HUDI_ZK_PORT", str(DEFAULT_ZK_PORT))),
            zookeeper_base_path=os.getenv("HUDI_ZK_BASE_PATH", DEFAULT_ZK_BASE_PATH),
            zookeeper_lock_key=os.getenv("HUDI_ZK_LOCK_KEY", DEFAULT_ZK_LOCK_KEY),
        )

    @property
    def dataset_ident(self) -> str:
        return f"{self.catalog_name}.{self.dataset_table}"

    @property
    def table_name(self) -> str:
        return self.dataset_table.split(".")[-1]

    @property
    def table_path(self) -> str:
        namespace = "/".join(self.dataset_ident.split(".")[:-1])
        return f"{self.warehouse.rstrip('/')}/{namespace}/{self.table_name}"


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


def schema_hash_for(columns: list[str] = HUDI_SAMPLE_COLUMNS) -> str:
    return sha256_text(stable_json({"columns": columns}))


def _source_updated_at(row: dict[str, Any]) -> datetime:
    prediction_timestamp = row.get("prediction_timestamp")
    try:
        missing_prediction_timestamp = prediction_timestamp is None or pd.isna(prediction_timestamp)
    except (TypeError, ValueError):
        missing_prediction_timestamp = prediction_timestamp is None
    if not missing_prediction_timestamp:
        timestamp = pd.Timestamp(prediction_timestamp)
    else:
        timestamp = pd.Timestamp(int(row.get("event_time", 0)), unit="s", tz="UTC")
    if timestamp.tzinfo is None:
        timestamp = timestamp.tz_localize("UTC")
    else:
        timestamp = timestamp.tz_convert("UTC")
    return timestamp.to_pydatetime()


def _validate_sample_keys(frame: pd.DataFrame) -> None:
    missing_columns = [column for column in HUDI_RECORD_KEY_COLUMNS if column not in frame.columns]
    if missing_columns:
        raise ValueError(f"Hudi sample data is missing record key columns: {missing_columns}")

    invalid_impression = frame["impression_id"].isna() | frame["impression_id"].astype(str).str.strip().eq("")
    invalid_target = frame["target_item_id"].isna()
    if bool((invalid_impression | invalid_target).any()):
        raise ValueError("Hudi record keys impression_id and target_item_id must be non-null")

    duplicates = frame.duplicated(HUDI_RECORD_KEY_COLUMNS, keep=False)
    if bool(duplicates.any()):
        duplicate_keys = (
            frame.loc[duplicates, HUDI_RECORD_KEY_COLUMNS]
            .drop_duplicates()
            .head(10)
            .to_dict(orient="records")
        )
        raise ValueError(f"Hudi record keys must be unique within a dataset snapshot: {duplicate_keys}")


def to_versioned_samples(splits: dict[str, list[dict[str, Any]]]) -> pd.DataFrame:
    records: list[dict[str, Any]] = []
    for split, rows in splits.items():
        for row in rows:
            normalized = dict(row)
            event_time = int(normalized.get("event_time", 0))
            impression_id = normalized.get("impression_id")
            target_item_id = normalized.get("target_item_id")
            record = {
                column: normalized.get(column)
                for column in MODEL_SAMPLE_COLUMNS
            }
            record.update(
                {
                    "impression_id": (
                        str(impression_id)
                        if impression_id is not None
                        else None
                    ),
                    "target_item_id": (
                        int(target_item_id)
                        if target_item_id is not None
                        else None
                    ),
                    "event_timestamp": datetime.fromtimestamp(event_time, tz=timezone.utc),
                    "split": split,
                    "source_updated_at": _source_updated_at(normalized),
                    "_hoodie_is_deleted": False,
                }
            )
            records.append(record)

    if not records:
        raise ValueError("Refusing to replace the Hudi dataset state from an empty source snapshot")
    frame = pd.DataFrame(records)[HUDI_SAMPLE_COLUMNS]
    _validate_sample_keys(frame)
    return frame


def split_counts(splits: dict[str, list[dict[str, Any]]]) -> dict[str, int]:
    return {split: len(rows) for split, rows in splits.items()}


def _spark_session(config: HudiConfig):
    from pyspark.sql import SparkSession

    return (
        SparkSession.builder.appName("recsys-bst-dataset-versioning")
        .config("spark.serializer", "org.apache.spark.serializer.KryoSerializer")
        .config("spark.sql.extensions", "org.apache.spark.sql.hudi.HoodieSparkSessionExtension")
        .config("spark.sql.catalog.spark_catalog", "org.apache.spark.sql.hudi.catalog.HoodieCatalog")
        .config("spark.hadoop.fs.s3a.endpoint", os.getenv("MINIO_ENDPOINT", "http://minio:9000"))
        .config("spark.hadoop.fs.s3a.access.key", os.getenv("AWS_ACCESS_KEY_ID", os.getenv("MINIO_ROOT_USER", "minio")))
        .config(
            "spark.hadoop.fs.s3a.secret.key",
            os.getenv("AWS_SECRET_ACCESS_KEY", os.getenv("MINIO_ROOT_PASSWORD", "minio123")),
        )
        .config("spark.hadoop.fs.s3a.path.style.access", "true")
        .config("spark.hadoop.fs.s3a.connection.ssl.enabled", "false")
        .getOrCreate()
    )


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
        BooleanType,
        LongType,
        StringType,
        StructField,
        StructType,
        TimestampType,
    )

    return StructType(
        [
            StructField("impression_id", StringType(), False),
            StructField("request_id", StringType(), True),
            StructField("user_id", LongType(), True),
            StructField("hist_item_id", ArrayType(LongType()), True),
            StructField("hist_event_type", ArrayType(LongType()), True),
            StructField("hist_category", ArrayType(LongType()), True),
            StructField("hist_brand", ArrayType(LongType()), True),
            StructField("hist_price_bucket", ArrayType(LongType()), True),
            StructField("hist_time", ArrayType(LongType()), True),
            StructField("target_item_id", LongType(), False),
            StructField("target_category", LongType(), True),
            StructField("target_brand", LongType(), True),
            StructField("target_price_bucket", LongType(), True),
            StructField("event_time", LongType(), True),
            StructField("label", LongType(), True),
            StructField("event_timestamp", TimestampType(), True),
            StructField("split", StringType(), False),
            StructField("source_updated_at", TimestampType(), False),
            StructField("_hoodie_is_deleted", BooleanType(), False),
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
        for column in ("event_timestamp", "source_updated_at"):
            record[column] = _spark_timestamp(record.get(column))
        for column in SEQUENCE_SAMPLE_COLUMNS:
            values = record.get(column) or []
            record[column] = [int(value) for value in values]
        for column in (
            "user_id",
            "target_item_id",
            "label",
            "target_category",
            "target_brand",
            "target_price_bucket",
            "event_time",
        ):
            value = record.get(column)
            if value is not None:
                record[column] = int(value)
        record["_hoodie_is_deleted"] = bool(record.get("_hoodie_is_deleted", False))
        records.append(record)
    return records
# apache hudi

def _hudi_options(
    table_name: str,
    config: HudiConfig,
    *,
    dataset_run_id: str,
    processing_code: str,
    feature_service_version: str,
) -> dict[str, str]:
    options = {
        "hoodie.table.name": table_name,
        "hoodie.datasource.write.table.name": table_name,
        "hoodie.datasource.write.table.type": "COPY_ON_WRITE",
        "hoodie.datasource.write.operation": "upsert",
        "hoodie.datasource.write.reconcile.schema": "true",
        "hoodie.datasource.write.recordkey.field": ",".join(HUDI_RECORD_KEY_COLUMNS),
        "hoodie.datasource.write.precombine.field": "source_updated_at",
        "hoodie.datasource.write.partitionpath.field": "split",
        "hoodie.datasource.write.hive_style_partitioning": "true",
        "hoodie.datasource.write.keygenerator.class": "org.apache.hudi.keygen.ComplexKeyGenerator",
        "hoodie.index.type": "GLOBAL_BLOOM",
        "hoodie.bloom.index.update.partition.path": "true",
        "hoodie.clean.policy": "KEEP_LATEST_BY_HOURS",
        "hoodie.clean.hours.retained": str(config.clean_hours_retained),
        "hoodie.clean.automatic": "true",
        "hoodie.datasource.write.commitmeta.key.prefix": "recsys_",
        "recsys_dataset_run_id": dataset_run_id,
        "recsys_processing_code_version": processing_code,
        "recsys_feature_service_version": feature_service_version,
    }
    if config.occ_enabled:
        options.update(
            {
                "hoodie.write.concurrency.mode": "optimistic_concurrency_control",
                "hoodie.write.lock.provider": "org.apache.hudi.client.transaction.lock.ZookeeperBasedLockProvider",
                "hoodie.write.lock.zookeeper.url": config.zookeeper_url,
                "hoodie.write.lock.zookeeper.port": str(config.zookeeper_port),
                "hoodie.write.lock.zookeeper.base_path": config.zookeeper_base_path,
                "hoodie.write.lock.zookeeper.lock_key": config.zookeeper_lock_key,
                "hoodie.cleaner.policy.failed.writes": "LAZY",
            }
        )
    return options


def _read_hudi_table(spark, table_path: str, hudi_instant: str | None = None):
    reader = spark.read.format("hudi")
    if hudi_instant:
        reader = reader.option("as.of.instant", hudi_instant)
    return reader.load(table_path)


def _hudi_table_exists(spark, table_path: str) -> bool:
    properties_path = spark._jvm.org.apache.hadoop.fs.Path(
        f"{table_path.rstrip('/')}/.hoodie/hoodie.properties"
    )
    filesystem = properties_path.getFileSystem(
        spark._jsc.hadoopConfiguration()
    )
    return bool(filesystem.exists(properties_path))


def _build_hudi_delta(spark, incoming, table_path: str, run_timestamp: datetime):
    from pyspark.sql import functions as F

    if not _hudi_table_exists(spark, table_path):
        return incoming, False, 0
    existing = _read_hudi_table(spark, table_path).select(*HUDI_SAMPLE_COLUMNS)

    current_keys = incoming.select(*HUDI_RECORD_KEY_COLUMNS).dropDuplicates(HUDI_RECORD_KEY_COLUMNS)
    deletes = (
        existing.join(current_keys, on=HUDI_RECORD_KEY_COLUMNS, how="left_anti")
        .withColumn("source_updated_at", F.lit(_spark_timestamp(run_timestamp)).cast("timestamp"))
        .withColumn("_hoodie_is_deleted", F.lit(True))
        .select(*HUDI_SAMPLE_COLUMNS)
    )
    deletes = deletes.persist()
    try:
        delete_rows = deletes.count()
        delta = incoming.unionByName(deletes)
    finally:
        deletes.unpersist()
    return delta, True, delete_rows


def _instant_requested_time(instant) -> str:
    for method_name in ("requestedTime", "getTimestamp"):
        try:
            return str(getattr(instant, method_name)())
        except Exception:
            continue
    raise RuntimeError("Unable to read a Hudi instant timestamp")


def _completed_commit_for_run(spark, table_path: str, dataset_run_id: str) -> str | None:
    try:
        jvm = spark._jvm
        hadoop_conf = spark._jsc.hadoopConfiguration()
        try:
            storage_conf = jvm.org.apache.hudi.storage.hadoop.HadoopStorageConfiguration(hadoop_conf)
        except Exception:
            storage_conf = hadoop_conf
        meta_client = (
            jvm.org.apache.hudi.common.table.HoodieTableMetaClient.builder()
            .setConf(storage_conf)
            .setBasePath(table_path)
            .build()
        )
        timeline = meta_client.reloadActiveTimeline().getCommitsTimeline().filterCompletedInstants()
        iterator = timeline.getReverseOrderedInstants().iterator()
        while iterator.hasNext():
            instant = iterator.next()
            metadata = timeline.readCommitMetadata(instant)
            extra_metadata = metadata.getExtraMetadata()
            if str(extra_metadata.get("recsys_dataset_run_id")) == dataset_run_id:
                return _instant_requested_time(instant)
    except Exception:
        return None
    return None


def _snapshot_counts(snapshot) -> tuple[int, dict[str, int]]:
    rows = snapshot.groupBy("split").count().collect()
    split_row_counts = {str(row["split"]): int(row["count"]) for row in rows}
    return sum(split_row_counts.values()), {
        split: split_row_counts.get(split, 0)
        for split in ("train", "val", "test")
    }


def _write_jsonl_from_snapshot(snapshot, split: str, output_path: Path) -> int:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    rows = (
        snapshot.where(snapshot["split"] == split)
        .select(*MODEL_SAMPLE_COLUMNS)
        .orderBy("event_time", "impression_id", "target_item_id")
        .collect()
    )
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
    *,
    processing_code: str,
    feature_service_version: str,
) -> dict[str, Any]:
    if samples.empty:
        raise ValueError("Refusing to write an empty Hudi dataset snapshot")
    _validate_sample_keys(samples)
    ensure_warehouse_bucket(config.warehouse)
    spark = _spark_session(config)
    try:
        output = Path(output_dir)
        started = time.perf_counter()
        table_path = config.table_path
        incoming = spark.createDataFrame(_spark_safe_records(samples), schema=_sample_schema())
        upsert_rows = len(samples)
        hudi_instant = (
            _completed_commit_for_run(
                spark,
                table_path,
                dataset_run_id,
            )
            if _hudi_table_exists(spark, table_path)
            else None
        )
        reused_completed_run = hudi_instant is not None
        if reused_completed_run:
            compared_with_snapshot = True
            delete_rows = 0
            write_rows = 0
            commit_latency_ms = 0.0
        else:
            run_timestamp = datetime.now(timezone.utc)
            delta, compared_with_snapshot, delete_rows = _build_hudi_delta(
                spark,
                incoming,
                table_path,
                run_timestamp,
            )
            delta = delta.persist()
            try:
                write_rows = delta.count()
                write_started = time.perf_counter()
                (
                    delta.write.format("hudi")
                    .options(
                        **_hudi_options(
                            config.table_name,
                            config,
                            dataset_run_id=dataset_run_id,
                            processing_code=processing_code,
                            feature_service_version=feature_service_version,
                        )
                    )
                    .mode("append")
                    .save(table_path)
                )
                commit_latency_ms = round(
                    (time.perf_counter() - write_started) * 1000,
                    3,
                )
            finally:
                delta.unpersist()
            hudi_instant = _completed_commit_for_run(
                spark,
                table_path,
                dataset_run_id,
            )
        if not hudi_instant:
            raise RuntimeError(
                f"Hudi write completed but no completed commit was found for dataset_run_id={dataset_run_id}"
            )

        snapshot = _read_hudi_table(spark, table_path, hudi_instant).persist()
        try:
            snapshot_rows, split_row_counts = _snapshot_counts(snapshot)
            jsonl_started = time.perf_counter()
            jsonl_counts = {
                split: _write_jsonl_from_snapshot(snapshot, split, output / f"{split}.jsonl")
                for split in ("train", "val", "test")
            }
            jsonl_latency_ms = round((time.perf_counter() - jsonl_started) * 1000, 3)
        finally:
            snapshot.unpersist()

        total_latency_ms = round((time.perf_counter() - started) * 1000, 3)
        return {
            "enabled": True,
            "storage": "hudi",
            "catalog_name": config.catalog_name,
            "warehouse": config.warehouse,
            "operation": "upsert",
            "table": {
                "name": config.dataset_ident,
                "path": table_path,
                "hudi_instant": hudi_instant,
                "input_rows": upsert_rows,
                "upsert_rows": upsert_rows,
                "delete_rows": delete_rows,
                "write_rows": write_rows,
                "snapshot_rows": snapshot_rows,
                "split_rows": split_row_counts,
                "compared_with_snapshot": compared_with_snapshot,
                "reused_completed_run": reused_completed_run,
            },
            "jsonl_counts": jsonl_counts,
            "latency_ms": {
                "commit": commit_latency_ms,
                "jsonl_export": jsonl_latency_ms,
                "total": total_latency_ms,
            },
        }
    finally:
        spark.stop()


def local_dataset_version_metadata(
    output_dir: str | Path,
    splits: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    output = Path(output_dir)
    split_row_counts = split_counts(splits)
    return {
        "enabled": False,
        "storage": "local",
        "operation": "local_export",
        "table": {
            "name": f"{DEFAULT_CATALOG_NAME}.{DATASET_TABLE}",
            "path": "",
            "hudi_instant": None,
            "input_rows": sum(split_row_counts.values()),
            "upsert_rows": 0,
            "delete_rows": 0,
            "write_rows": 0,
            "snapshot_rows": sum(split_row_counts.values()),
            "split_rows": split_row_counts,
            "compared_with_snapshot": False,
            "reused_completed_run": False,
        },
        "jsonl_counts": split_row_counts,
        "jsonl_paths": {
            split: str(output / f"{split}.jsonl")
            for split in ("train", "val", "test")
        },
        "latency_ms": {},
    }
