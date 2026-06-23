from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pandas as pd

from ingest.minio_raw_reader import read_generator_run
from .connection import connect
from .schemas import TableSpec
from .writer import ensure_warehouse, upsert_frame


PRIMARY_KEYS = {
    "users": ("user_id",),
    "user_preferences": ("user_id", "category_id", "brand_id"),
    "products": ("product_id",),
    "product_snapshots": ("product_id", "valid_from"),
    "sessions": ("session_id",),
    "recommendation_requests": ("request_id",),
    "impressions": ("impression_id",),
    "behavior_events": ("event_id", "payload_hash"),
    "orders": ("order_id",),
    "order_items": ("order_item_id",),
}


def infer_sql_type(series: pd.Series) -> str:
    if pd.api.types.is_bool_dtype(series):
        return "BOOLEAN"
    if pd.api.types.is_integer_dtype(series):
        return "BIGINT"
    if pd.api.types.is_float_dtype(series):
        return "DOUBLE PRECISION"
    if pd.api.types.is_datetime64_any_dtype(series):
        return "TIMESTAMPTZ"
    if pd.api.types.is_object_dtype(series) and series.dropna().map(lambda value: hasattr(value, "as_tuple")).any():
        return "DOUBLE PRECISION"
    return "TEXT"


def table_spec_from_frame(table_name: str, frame: pd.DataFrame) -> TableSpec:
    columns = {column: infer_sql_type(frame[column]) for column in frame.columns}
    return TableSpec(
        schema="staging",
        name=table_name,
        columns=columns,
        primary_key=PRIMARY_KEYS[table_name],
    )


def normalize_frame(frame: pd.DataFrame) -> pd.DataFrame:
    normalized = frame.copy()
    for column in normalized.columns:
        if normalized[column].dtype == "object":
            normalized[column] = normalized[column].map(
                lambda value: str(value) if hasattr(value, "as_tuple") else value
            )
    return normalized


def normalize_staging_frame(table_name: str, frame: pd.DataFrame) -> pd.DataFrame:
    normalized = normalize_frame(frame)
    if table_name == "user_preferences":
        for column in ("category_id", "brand_id"):
            if column in normalized.columns:
                normalized[column] = pd.to_numeric(normalized[column], errors="coerce").fillna(0).astype("int64")
    return normalized


def load_historical_run_to_staging(run_path: str | Path, limit_per_table: int | None = None) -> dict[str, int]:
    tables = read_generator_run(run_path)
    normalized_tables = {
        name: normalize_staging_frame(name, frame)
        for name, frame in tables.items()
    }
    specs = [table_spec_from_frame(name, frame) for name, frame in normalized_tables.items()]
    counts: dict[str, int] = {}
    with connect() as connection:
        ensure_warehouse(connection, specs)
        for spec in specs:
            frame = normalized_tables[spec.name]
            if limit_per_table is not None:
                frame = frame.head(limit_per_table)
            counts[spec.qualified_name] = upsert_frame(connection, spec, frame)
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description="Load generated raw parquet into warehouse staging schemas.")
    parser.add_argument("--run-path", default="s3://recsys-lake/raw/test_10k_seed42")
    parser.add_argument("--limit-per-table", type=int, default=None)
    args = parser.parse_args()
    print(
        json.dumps(
            load_historical_run_to_staging(args.run_path, limit_per_table=args.limit_per_table),
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
