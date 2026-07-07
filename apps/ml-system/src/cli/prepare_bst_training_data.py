from __future__ import annotations

import argparse
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import numpy as np
import pandas as pd

from lineage.dataset_versioning import (
    DEFAULT_CATALOG_NAME,
    DEFAULT_WAREHOUSE,
    HudiConfig,
    commit_samples_to_hudi,
    local_dataset_version_metadata,
    processing_code_version as resolve_processing_code_version,
    schema_hash_for,
    timestamp_run_id,
    to_versioned_samples,
)

DEFAULT_FEATURE_SERVICE_NAME = "bst_ranking_v1"
DEFAULT_OFFLINE_FEATURE_TABLE = "recsys_features.feature_store.ml_bst_training"

MODEL_COLUMNS = [
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

SEQUENCE_COLUMNS = [
    "hist_item_id",
    "hist_event_type",
    "hist_category",
    "hist_brand",
    "hist_price_bucket",
    "hist_time",
]

FEAST_FEATURE_REFS = [
    "user_sequence_features:hist_item_ids",
    "user_sequence_features:hist_event_type_ids",
    "user_sequence_features:hist_category_ids",
    "user_sequence_features:hist_brand_ids",
    "user_sequence_features:hist_price_bucket_ids",
    "user_sequence_features:hist_event_timestamps",
    "user_aggregate_features:views_30m",
    "user_aggregate_features:carts_30m",
    "user_aggregate_features:purchases_24h",
    "item_features:category_id",
    "item_features:brand_id",
    "item_features:price_bucket",
]


def get_time_buckets(prediction_ts: datetime, history_timestamps: list[datetime]) -> list[int]:
    buckets: list[int] = []
    for timestamp in history_timestamps:
        delta_seconds = max(int((prediction_ts - timestamp).total_seconds()), 0)
        buckets.append(min(delta_seconds // 300 + 1, 24 * 12 * 365))
    return buckets


def _to_int(value: Any, default: int = 0) -> int:
    if value is None or pd.isna(value):
        return default
    return int(value)


def _to_int_list(value: Any) -> list[int]:
    if value is None:
        return []
    if isinstance(value, np.ndarray):
        value = value.tolist()
    elif isinstance(value, str):
        value = json.loads(value) if value.strip().startswith("[") else []
    elif not isinstance(value, (list, tuple)):
        return []
    return [int(item) for item in value if item is not None and not pd.isna(item)]


def _to_str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, np.ndarray):
        value = value.tolist()
    elif isinstance(value, str):
        value = json.loads(value) if value.strip().startswith("[") else [value]
    elif not isinstance(value, (list, tuple)):
        return []
    return [str(item) for item in value if item is not None and not pd.isna(item)]


def _prediction_timestamps(frame: pd.DataFrame) -> pd.Series:
    if "prediction_timestamp" in frame.columns:
        return pd.to_datetime(frame["prediction_timestamp"], utc=True)
    if "event_time" in frame.columns:
        return pd.to_datetime(frame["event_time"], unit="s", utc=True)
    raise ValueError("Entity table must include prediction_timestamp or event_time")


def _canonical_entity_frame(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        raise ValueError("No rows found in Feast entity table")
    frame = frame.reset_index(drop=True)
    entity = pd.DataFrame()
    entity["row_id"] = range(len(frame))
    entity["impression_id"] = frame.get("impression_id", pd.Series([""] * len(frame))).astype(str)
    entity["request_id"] = frame.get("request_id", pd.Series([""] * len(frame))).astype(str)
    entity["user_id"] = frame["user_id"].astype(int)
    if "candidate_product_id" in frame.columns:
        entity["product_id"] = frame["candidate_product_id"].astype(int)
    elif "target_item_id" in frame.columns:
        entity["product_id"] = frame["target_item_id"].astype(int)
    else:
        raise ValueError("Entity table must include candidate_product_id or target_item_id")
    entity["event_timestamp"] = _prediction_timestamps(frame)
    entity["label"] = frame["label"].fillna(0).astype(int) if "label" in frame.columns else 0
    return entity


def _looks_like_table_identifier(value: str) -> bool:
    return "://" not in value and "/" not in value and value.count(".") >= 2


def _read_iceberg_table_as_pandas(table_name: str, catalog_name: str, warehouse: str) -> pd.DataFrame:
    spark = _spark_session_for_offline_features(catalog_name, warehouse)
    try:
        return spark.table(table_name).toPandas()
    finally:
        spark.stop()


def _read_postgres_table_as_pandas(table_uri: str) -> pd.DataFrame:
    import psycopg
    from psycopg import sql

    parsed = urlparse(table_uri)
    path = parsed.path.lstrip("/")
    if "/" in path:
        database, table_ref = path.split("/", 1)
    else:
        database = os.getenv("FEAST_POSTGRES_DB", "feature_store")
        table_ref = path
    if "." in table_ref:
        schema, table = table_ref.rsplit(".", 1)
    else:
        schema = os.getenv("FEAST_POSTGRES_SCHEMA", "feature_store")
        table = table_ref
    with psycopg.connect(
        host=parsed.hostname or os.getenv("FEAST_POSTGRES_HOST", "feature-postgres"),
        port=parsed.port or int(os.getenv("FEAST_POSTGRES_PORT", "5432")),
        dbname=database or os.getenv("FEAST_POSTGRES_DB", "feature_store"),
        user=unquote(parsed.username) if parsed.username else os.getenv("FEAST_POSTGRES_USER", "feast"),
        password=unquote(parsed.password) if parsed.password else os.getenv("FEAST_POSTGRES_PASSWORD", "feast"),
        sslmode=os.getenv("FEAST_POSTGRES_SSLMODE", "disable"),
    ) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = %s AND table_name = %s
                """,
                (schema, table),
            )
            table_columns = {row[0] for row in cur.fetchall()}
            entity_columns = [
                "impression_id",
                "request_id",
                "user_id",
                "candidate_product_id",
                "target_item_id",
                "prediction_timestamp",
                "event_time",
                "label",
            ]
            selected_columns = [column for column in entity_columns if column in table_columns]
            if {"user_id", "label"}.issubset(selected_columns) and (
                "candidate_product_id" in selected_columns or "target_item_id" in selected_columns
            ):
                select_expr = sql.SQL(", ").join(sql.Identifier(column) for column in selected_columns)
            else:
                select_expr = sql.SQL("*")
            query = sql.SQL("SELECT {} FROM {}.{}").format(
                select_expr,
                sql.Identifier(schema),
                sql.Identifier(table),
            )
            params: tuple[Any, ...] = ()
            if "prediction_timestamp" in table_columns:
                query += sql.SQL(" WHERE prediction_timestamp >= %s AND prediction_timestamp < %s")
                params = (
                    datetime(1970, 1, 1, tzinfo=timezone.utc),
                    datetime(2100, 1, 1, tzinfo=timezone.utc),
                )
            cur.execute(query, params)
            rows = cur.fetchall()
            columns = [column.name for column in cur.description or []]
    return pd.DataFrame(rows, columns=columns)


def _read_feature_table(path: str, *, iceberg_catalog_name: str, iceberg_warehouse: str) -> pd.DataFrame:
    if path.startswith(("postgresql://", "postgres://")):
        return _read_postgres_table_as_pandas(path)
    source = Path(path)
    if source.is_dir():
        return pd.read_parquet(source)
    if source.exists():
        return pd.read_parquet(source)
    if _looks_like_table_identifier(path):
        return _read_iceberg_table_as_pandas(path, iceberg_catalog_name, iceberg_warehouse)
    raise FileNotFoundError(f"Feature table path does not exist: {path}")


def _feature_col(feature_view: str, feature_name: str) -> str:
    return f"{feature_view}__{feature_name}"


def _row_timestamp(value: Any) -> pd.Timestamp:
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None:
        timestamp = timestamp.tz_localize("UTC")
    return timestamp


def _feast_historical_to_bst_frame(
    entities: pd.DataFrame,
    historical: pd.DataFrame,
    max_history_len: int,
) -> pd.DataFrame:
    if "row_id" not in historical.columns:
        historical = historical.copy()
        historical["row_id"] = range(len(historical))
    feature_columns = [column for column in historical.columns if "__" in column or column == "row_id"]
    joined = entities.merge(historical[feature_columns], on="row_id", how="left")

    rows: list[dict[str, Any]] = []
    sequence_prefix = "user_sequence_features"
    item_prefix = "item_features"
    aggregate_prefix = "user_aggregate_features"
    for _, row in joined.sort_values("event_timestamp").iterrows():
        prediction_ts = _row_timestamp(row["event_timestamp"])
        hist_timestamps = []
        for value in _to_str_list(row.get(_feature_col(sequence_prefix, "hist_event_timestamps"))):
            timestamp = _row_timestamp(value)
            if timestamp < prediction_ts:
                hist_timestamps.append(timestamp.to_pydatetime())
        hist_len = min(len(hist_timestamps), max_history_len)
        hist_timestamps = hist_timestamps[-hist_len:] if hist_len else []

        def history_values(name: str) -> list[int]:
            values = _to_int_list(row.get(_feature_col(sequence_prefix, name)))
            return values[-hist_len:] if hist_len else []

        rows.append(
            {
                "impression_id": row.get("impression_id", ""),
                "request_id": row.get("request_id", ""),
                "user_id": _to_int(row.get("user_id")),
                "hist_item_id": history_values("hist_item_ids"),
                "hist_event_type": history_values("hist_event_type_ids"),
                "hist_category": history_values("hist_category_ids"),
                "hist_brand": history_values("hist_brand_ids"),
                "hist_price_bucket": history_values("hist_price_bucket_ids"),
                "hist_time": get_time_buckets(prediction_ts.to_pydatetime(), hist_timestamps),
                "target_item_id": _to_int(row.get("product_id")),
                "target_category": _to_int(row.get(_feature_col(item_prefix, "category_id"))),
                "target_brand": _to_int(row.get(_feature_col(item_prefix, "brand_id"))),
                "target_price_bucket": _to_int(row.get(_feature_col(item_prefix, "price_bucket"))),
                "event_time": int(prediction_ts.timestamp()),
                "prediction_timestamp": prediction_ts,
                "label": _to_int(row.get("label")),
                "views_30m": _to_int(row.get(_feature_col(aggregate_prefix, "views_30m"))),
                "carts_30m": _to_int(row.get(_feature_col(aggregate_prefix, "carts_30m"))),
                "purchases_24h": _to_int(row.get(_feature_col(aggregate_prefix, "purchases_24h"))),
            }
        )
    return pd.DataFrame(rows)


def _apply_feast_repo(repo_path: str | Path) -> None:
    from feature_store.feast_registry import apply_feature_repo

    try:
        apply_feature_repo(repo_path)
    except subprocess.CalledProcessError as exc:
        if exc.stdout:
            print(exc.stdout)
        if exc.stderr:
            print(exc.stderr)
        raise


def build_bst_training_table_from_feast(
    entity_input_path: str,
    feast_repo_path: str | Path,
    max_history_len: int = 50,
    feast_offline_root: str | None = None,
    apply_feast_repo: bool = True,
    feature_service_name: str = DEFAULT_FEATURE_SERVICE_NAME,
    fallback_to_feature_refs: bool = True,
    iceberg_catalog_name: str = DEFAULT_CATALOG_NAME,
    iceberg_warehouse: str = DEFAULT_WAREHOUSE,
) -> pd.DataFrame:
    if feast_offline_root:
        os.environ["FEAST_OFFLINE_ROOT"] = feast_offline_root
    if apply_feast_repo:
        _apply_feast_repo(feast_repo_path)

    from feast import FeatureStore

    entities = _canonical_entity_frame(
        _read_feature_table(
            entity_input_path,
            iceberg_catalog_name=iceberg_catalog_name,
            iceberg_warehouse=iceberg_warehouse,
        )
    )
    store = FeatureStore(repo_path=str(feast_repo_path))
    features: Any = FEAST_FEATURE_REFS
    if feature_service_name:
        try:
            features = store.get_feature_service(feature_service_name)
        except Exception:
            if not fallback_to_feature_refs:
                raise
            features = FEAST_FEATURE_REFS
    historical = store.get_historical_features(
        entity_df=entities,
        features=features,
        full_feature_names=True,
    ).to_df()
    return _feast_historical_to_bst_frame(entities, historical, max_history_len=max_history_len)


def _spark_session_for_offline_features(catalog_name: str, warehouse: str):
    from pyspark.sql import SparkSession

    return (
        SparkSession.builder.appName("recsys-read-offline-feature-store-for-bst")
        .config("spark.sql.extensions", "org.apache.iceberg.spark.extensions.IcebergSparkSessionExtensions")
        .config(f"spark.sql.catalog.{catalog_name}", "org.apache.iceberg.spark.SparkCatalog")
        .config(f"spark.sql.catalog.{catalog_name}.type", "hadoop")
        .config(f"spark.sql.catalog.{catalog_name}.warehouse", warehouse)
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


def build_bst_training_table_from_offline_feature_store(
    offline_feature_table: str = DEFAULT_OFFLINE_FEATURE_TABLE,
    *,
    iceberg_catalog_name: str = DEFAULT_CATALOG_NAME,
    iceberg_warehouse: str = DEFAULT_WAREHOUSE,
) -> pd.DataFrame:
    spark = _spark_session_for_offline_features(iceberg_catalog_name, iceberg_warehouse)
    try:
        frame = spark.table(offline_feature_table)
        missing = [column for column in MODEL_COLUMNS if column not in frame.columns]
        if missing:
            raise ValueError(f"Offline feature table {offline_feature_table} is missing BST columns: {missing}")
        selected = frame.select(*MODEL_COLUMNS, "impression_id", "request_id", "prediction_timestamp")
        return selected.toPandas()
    finally:
        spark.stop()


class TrainingDataService:
    def __init__(
        self,
        *,
        feast_repo_path: str | Path = "apps/data-platform/feature-store/feature_repo",
        feast_offline_root: str | None = None,
        apply_feast_repo: bool = True,
        feature_service_name: str = DEFAULT_FEATURE_SERVICE_NAME,
        offline_feature_table: str = DEFAULT_OFFLINE_FEATURE_TABLE,
        max_history_len: int = 50,
        iceberg_catalog_name: str = DEFAULT_CATALOG_NAME,
        iceberg_warehouse: str = DEFAULT_WAREHOUSE,
    ) -> None:
        self.feast_repo_path = feast_repo_path
        self.feast_offline_root = feast_offline_root
        self.apply_feast_repo = apply_feast_repo
        self.feature_service_name = feature_service_name
        self.offline_feature_table = offline_feature_table
        self.max_history_len = max_history_len
        self.iceberg_catalog_name = iceberg_catalog_name
        self.iceberg_warehouse = iceberg_warehouse

    def read_training_table(self, source: str, *, entity_input_path: str) -> pd.DataFrame:
        if source == "offline_feature_store":
            frame = self.read_from_offline_feature_store(self.offline_feature_table)
        elif source == "feast":
            frame = self.read_from_feast(entity_input_path)
        else:
            raise ValueError(f"Unsupported feature_source: {source}")
        self.validate_schema(frame)
        return frame

    def read_from_feast(self, entity_input_path: str) -> pd.DataFrame:
        return build_bst_training_table_from_feast(
            entity_input_path=entity_input_path,
            feast_repo_path=self.feast_repo_path,
            max_history_len=self.max_history_len,
            feast_offline_root=self.feast_offline_root,
            apply_feast_repo=self.apply_feast_repo,
            feature_service_name=self.feature_service_name,
            iceberg_catalog_name=self.iceberg_catalog_name,
            iceberg_warehouse=self.iceberg_warehouse,
        )

    def read_from_offline_feature_store(self, table_name: str) -> pd.DataFrame:
        return build_bst_training_table_from_offline_feature_store(
            table_name,
            iceberg_catalog_name=self.iceberg_catalog_name,
            iceberg_warehouse=self.iceberg_warehouse,
        )

    def validate_schema(self, df: pd.DataFrame) -> None:
        missing = [column for column in MODEL_COLUMNS if column not in df.columns]
        if missing:
            raise ValueError(f"BST training dataframe is missing columns: {missing}")

    def canonicalize_entities(self, df: pd.DataFrame) -> pd.DataFrame:
        return _canonical_entity_frame(df)

    def build_bst_frame(self, entities: pd.DataFrame, historical: pd.DataFrame) -> pd.DataFrame:
        frame = _feast_historical_to_bst_frame(entities, historical, max_history_len=self.max_history_len)
        self.validate_schema(frame)
        return frame


def _normalize_row(row: pd.Series, max_history_len: int) -> dict[str, Any]:
    payload = {column: row.get(column) for column in MODEL_COLUMNS}
    payload["impression_id"] = str(row.get("impression_id", ""))
    payload["request_id"] = str(row.get("request_id", ""))
    if "prediction_timestamp" in row:
        payload["prediction_timestamp"] = str(row.get("prediction_timestamp"))
    sequences = {column: _to_int_list(payload[column]) for column in SEQUENCE_COLUMNS}
    hist_len = min(
        max((len(values) for values in sequences.values()), default=0),
        max_history_len,
    )
    for column, values in sequences.items():
        values = values[-hist_len:] if hist_len else []
        if len(values) < hist_len:
            values = ([0] * (hist_len - len(values))) + values
        payload[column] = values

    for column in [
        "user_id",
        "target_item_id",
        "target_category",
        "target_brand",
        "target_price_bucket",
        "event_time",
        "label",
    ]:
        payload[column] = _to_int(payload[column])

    return payload


def _write_jsonl(rows: list[dict[str, Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as file:
        for row in rows:
            payload = {column: row.get(column) for column in MODEL_COLUMNS}
            file.write(json.dumps(payload, separators=(",", ":")) + "\n")


def _bool_flag(value: str | bool | None, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return value.lower() in {"1", "true", "yes", "y", "on"}


def _registry_path(feast_repo_path: str | Path) -> str:
    return str(Path(feast_repo_path) / "data" / "registry.db")


def _write_json(path: str | Path, payload: dict[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")


def _dataset_metadata(
    *,
    output_dir: Path,
    dataset_run_id: str,
    entity_input_path: str,
    feast_repo_path: str | Path,
    feast_offline_root: str | None,
    feature_service_name: str,
    processing_code: str,
    split_counts: dict[str, int],
    hudi: dict[str, Any],
    max_history_len: int,
    feature_source: str,
    offline_feature_table: str,
) -> dict[str, Any]:
    training_table = hudi["tables"]["training"]
    evaluation_table = hudi["tables"]["evaluation"]
    return {
        "dataset_run_id": dataset_run_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "entity_input_path": entity_input_path,
        "feature_source": feature_source,
        "offline_feature_table": offline_feature_table,
        "feature_service_name": feature_service_name,
        "feast_repo_path": str(feast_repo_path),
        "feast_registry_path": _registry_path(feast_repo_path),
        "feast_offline_root": feast_offline_root or "",
        "processing_code_version": processing_code,
        "schema_hash": schema_hash_for(),
        "split_strategy": "temporal",
        "max_history_len": max_history_len,
        "hudi": hudi,
        "splits": {
            "train": {
                "row_count": split_counts.get("train", 0),
                "jsonl_path": str(output_dir / "train.jsonl"),
                "table": training_table["name"],
                "snapshot_id": training_table["snapshot_id"],
                "commit_time": training_table.get("commit_time"),
                "tag": training_table["tag"],
            },
            "val": {
                "row_count": split_counts.get("val", 0),
                "jsonl_path": str(output_dir / "val.jsonl"),
                "table": training_table["name"],
                "snapshot_id": training_table["snapshot_id"],
                "commit_time": training_table.get("commit_time"),
                "tag": training_table["tag"],
            },
            "test": {
                "row_count": split_counts.get("test", 0),
                "jsonl_path": str(output_dir / "test.jsonl"),
                "table": evaluation_table["name"],
                "snapshot_id": evaluation_table["snapshot_id"],
                "commit_time": evaluation_table.get("commit_time"),
                "tag": evaluation_table["tag"],
            },
        },
    }


class SplitService:
    def __init__(self, *, train_ratio: float = 0.8, val_ratio: float = 0.1, max_history_len: int = 50) -> None:
        self.train_ratio = train_ratio
        self.val_ratio = val_ratio
        self.max_history_len = max_history_len

    def sort_by_prediction_time(self, df: pd.DataFrame) -> pd.DataFrame:
        if "prediction_timestamp" in df.columns:
            return df.sort_values("prediction_timestamp")
        return df.sort_values("event_time")

    def normalize_row(self, row: pd.Series, max_history_len: int | None = None) -> dict[str, Any]:
        return _normalize_row(row, max_history_len=max_history_len or self.max_history_len)

    def get_split_boundaries(
        self,
        row_count: int,
        train_ratio: float | None = None,
        val_ratio: float | None = None,
    ) -> dict[str, int]:
        train_end = int(row_count * (self.train_ratio if train_ratio is None else train_ratio))
        val_end = train_end + int(row_count * (self.val_ratio if val_ratio is None else val_ratio))
        return {"train_end": train_end, "val_end": val_end}

    def split_by_time(self, rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
        boundaries = self.get_split_boundaries(len(rows))
        train_end = boundaries["train_end"]
        val_end = boundaries["val_end"]
        return {
            "train": rows[:train_end],
            "val": rows[train_end:val_end],
            "test": rows[val_end:],
        }

    def write_jsonl_splits(self, splits: dict[str, list[dict[str, Any]]], output_dir: Path) -> None:
        for split, split_rows in splits.items():
            _write_jsonl(split_rows, output_dir / f"{split}.jsonl")

    def write_dataset_metadata(
        self,
        *,
        output_dir: Path,
        dataset_run_id: str,
        entity_input_path: str,
        feast_repo_path: str | Path,
        feast_offline_root: str | None,
        feature_service_name: str,
        processing_code: str,
        splits: dict[str, list[dict[str, Any]]],
        hudi: dict[str, Any],
        feature_source: str,
        offline_feature_table: str,
        dataset_metadata_path: str | Path | None,
    ) -> dict[str, Any]:
        dataset_metadata = _dataset_metadata(
            output_dir=output_dir,
            dataset_run_id=dataset_run_id,
            entity_input_path=entity_input_path,
            feast_repo_path=feast_repo_path,
            feast_offline_root=feast_offline_root,
            feature_service_name=feature_service_name,
            processing_code=processing_code,
            split_counts={split: len(split_rows) for split, split_rows in splits.items()},
            hudi=hudi,
            max_history_len=self.max_history_len,
            feature_source=feature_source,
            offline_feature_table=offline_feature_table,
        )
        dataset_meta_target = Path(dataset_metadata_path) if dataset_metadata_path else output_dir / "dataset_version_meta.json"
        _write_json(dataset_meta_target, dataset_metadata)
        return dataset_metadata


def prepare_bst_jsonl_splits(
    entity_input_path: str,
    output_dir: str | Path,
    train_ratio: float = 0.8,
    val_ratio: float = 0.1,
    max_history_len: int = 50,
    feast_repo_path: str = "apps/data-platform/feature-store/feature_repo",
    feast_offline_root: str | None = None,
    apply_feast_repo: bool = True,
    feature_service_name: str = DEFAULT_FEATURE_SERVICE_NAME,
    feature_source: str = "offline_feature_store",
    offline_feature_table: str = DEFAULT_OFFLINE_FEATURE_TABLE,
    hudi_enabled: bool = False,
    hudi_warehouse: str = DEFAULT_WAREHOUSE,
    hudi_catalog_name: str = DEFAULT_CATALOG_NAME,
    iceberg_enabled: bool | None = None,
    iceberg_catalog_name: str = DEFAULT_CATALOG_NAME,
    iceberg_warehouse: str = DEFAULT_WAREHOUSE,
    dataset_run_id: str | None = None,
    dataset_metadata_path: str | Path | None = None,
    processing_code_version: str | None = None,
) -> dict[str, Any]:
    training_data = TrainingDataService(
        feast_repo_path=feast_repo_path,
        feast_offline_root=feast_offline_root,
        apply_feast_repo=apply_feast_repo,
        feature_service_name=feature_service_name,
        offline_feature_table=offline_feature_table,
        max_history_len=max_history_len,
        iceberg_catalog_name=iceberg_catalog_name,
        iceberg_warehouse=iceberg_warehouse,
    )
    split_service = SplitService(train_ratio=train_ratio, val_ratio=val_ratio, max_history_len=max_history_len)
    frame = training_data.read_training_table(feature_source, entity_input_path=entity_input_path)
    if frame.empty:
        raise ValueError(f"No rows found in BST training data from {entity_input_path}")

    frame = split_service.sort_by_prediction_time(frame)
    rows = [split_service.normalize_row(row) for _, row in frame.iterrows()]

    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    splits = split_service.split_by_time(rows)
    run_id = dataset_run_id or timestamp_run_id()
    processing_code = processing_code_version or resolve_processing_code_version()
    versioning_enabled = hudi_enabled if iceberg_enabled is None else hudi_enabled or iceberg_enabled
    if versioning_enabled:
        samples = to_versioned_samples(
            splits,
            dataset_run_id=run_id,
            feature_service_version=feature_service_name,
            processing_code=processing_code,
        )
        hudi_metadata = commit_samples_to_hudi(
            samples=samples,
            output_dir=output,
            dataset_run_id=run_id,
            config=HudiConfig(catalog_name=hudi_catalog_name, warehouse=hudi_warehouse),
        )
    else:
        split_service.write_jsonl_splits(splits, output)
        hudi_metadata = local_dataset_version_metadata(output, splits)

    dataset_metadata = split_service.write_dataset_metadata(
        output_dir=output,
        dataset_run_id=run_id,
        entity_input_path=entity_input_path,
        feast_repo_path=feast_repo_path,
        feast_offline_root=feast_offline_root,
        feature_service_name=feature_service_name,
        processing_code=processing_code,
        splits=splits,
        hudi=hudi_metadata,
        feature_source=feature_source,
        offline_feature_table=offline_feature_table,
        dataset_metadata_path=dataset_metadata_path,
    )
    dataset_meta_target = Path(dataset_metadata_path) if dataset_metadata_path else output / "dataset_version_meta.json"

    metadata = {
        "entity_input_path": entity_input_path,
        "output_dir": str(output),
        "feature_source": feature_source,
        "offline_feature_table": offline_feature_table,
        "feature_service_name": feature_service_name,
        "feast_repo_path": str(feast_repo_path),
        "feast_registry_path": _registry_path(feast_repo_path),
        "feast_offline_root": feast_offline_root or "",
        "feast_features": FEAST_FEATURE_REFS,
        "dataset_run_id": run_id,
        "dataset_metadata_path": str(dataset_meta_target),
        "processing_code_version": processing_code,
        "schema_hash": dataset_metadata["schema_hash"],
        "hudi": hudi_metadata,
        "versioning_latency_ms": hudi_metadata.get("latency_ms", {}),
        "total_rows": len(rows),
        "train_rows": len(splits["train"]),
        "val_rows": len(splits["val"]),
        "test_rows": len(splits["test"]),
        "split_strategy": "temporal",
        "max_history_len": max_history_len,
    }
    _write_json(output / "split_meta.json", metadata)
    return metadata


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare BST JSONL splits from the offline feature store")
    parser.add_argument(
        "--entity-input-path",
        default="data_platform/output/ml/offline/ml_ranking_labels",
        help="Ranking labels/entity dataframe used for Feast point-in-time feature retrieval.",
    )
    parser.add_argument(
        "--feast-repo-path",
        default="apps/data-platform/feature-store/feature_repo",
    )
    parser.add_argument("--feast-offline-root", default="")
    parser.add_argument("--skip-feast-apply", action="store_true")
    parser.add_argument("--output-dir", default="data_platform/output/ml/bst_split")
    parser.add_argument("--train-ratio", type=float, default=0.8)
    parser.add_argument("--val-ratio", type=float, default=0.1)
    parser.add_argument("--max-history-len", type=int, default=50)
    parser.add_argument("--metadata-path", default="")
    parser.add_argument("--feature-service-name", default=DEFAULT_FEATURE_SERVICE_NAME)
    parser.add_argument("--feature-source", choices=["offline_feature_store", "feast"], default="offline_feature_store")
    parser.add_argument("--offline-feature-table", default=os.getenv("OFFLINE_FEATURE_TABLE", DEFAULT_OFFLINE_FEATURE_TABLE))
    parser.add_argument("--hudi-enabled", default=os.getenv("HUDI_ENABLED", os.getenv("ICEBERG_ENABLED", "false")))
    parser.add_argument("--hudi-warehouse", default=os.getenv("HUDI_WAREHOUSE", DEFAULT_WAREHOUSE))
    parser.add_argument("--hudi-catalog-name", default=os.getenv("HUDI_CATALOG_NAME", DEFAULT_CATALOG_NAME))
    parser.add_argument("--iceberg-enabled", default=None)
    parser.add_argument("--iceberg-catalog-name", default=os.getenv("ICEBERG_CATALOG_NAME", DEFAULT_CATALOG_NAME))
    parser.add_argument("--iceberg-warehouse", default=os.getenv("ICEBERG_WAREHOUSE", DEFAULT_WAREHOUSE))
    parser.add_argument("--dataset-run-id", default="")
    parser.add_argument("--dataset-metadata-path", default="")
    parser.add_argument("--processing-code-version", default="")
    args = parser.parse_args()

    metadata = prepare_bst_jsonl_splits(
        entity_input_path=args.entity_input_path,
        output_dir=args.output_dir,
        train_ratio=args.train_ratio,
        val_ratio=args.val_ratio,
        max_history_len=args.max_history_len,
        feast_repo_path=args.feast_repo_path,
        feast_offline_root=args.feast_offline_root or None,
        apply_feast_repo=not args.skip_feast_apply,
        feature_service_name=args.feature_service_name,
        feature_source=args.feature_source,
        offline_feature_table=args.offline_feature_table,
        hudi_enabled=_bool_flag(args.hudi_enabled, default=False),
        hudi_warehouse=args.hudi_warehouse,
        hudi_catalog_name=args.hudi_catalog_name,
        iceberg_enabled=_bool_flag(args.iceberg_enabled, default=False) if args.iceberg_enabled is not None else None,
        iceberg_catalog_name=args.iceberg_catalog_name,
        iceberg_warehouse=args.iceberg_warehouse,
        dataset_run_id=args.dataset_run_id or None,
        dataset_metadata_path=args.dataset_metadata_path or None,
        processing_code_version=args.processing_code_version or None,
    )
    if args.metadata_path:
        _write_json(args.metadata_path, metadata)
    print(json.dumps(metadata, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
