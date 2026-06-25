from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from typing import Any, Iterable

import pandas as pd

from .schemas import TableSpec, WAREHOUSE_TABLES


def _quote(identifier: str) -> str:
    if not identifier.replace("_", "").isalnum():
        raise ValueError(f"Unsafe SQL identifier: {identifier}")
    return f'"{identifier}"'


def _qualified(table: TableSpec) -> str:
    return f"{_quote(table.schema)}.{_quote(table.name)}"


def create_schema_sql(schema: str) -> str:
    return f"CREATE SCHEMA IF NOT EXISTS {_quote(schema)}"


def create_table_sql(table: TableSpec) -> str:
    columns = [f"{_quote(name)} {data_type}" for name, data_type in table.columns.items()]
    if table.primary_key:
        columns.append("PRIMARY KEY (" + ", ".join(_quote(column) for column in table.primary_key) + ")")
    return f"CREATE TABLE IF NOT EXISTS {_qualified(table)} (\n  " + ",\n  ".join(columns) + "\n)"


def ensure_warehouse(connection: Any, tables: Iterable[TableSpec] = WAREHOUSE_TABLES) -> None:
    cursor = connection.cursor()
    seen_schemas = set()
    for table in tables:
        if table.schema not in seen_schemas:
            cursor.execute(create_schema_sql(table.schema))
            seen_schemas.add(table.schema)
        cursor.execute(create_table_sql(table))
        for column, data_type in table.columns.items():
            cursor.execute(
                f"ALTER TABLE {_qualified(table)} "
                f"ADD COLUMN IF NOT EXISTS {_quote(column)} {data_type}"
            )
    connection.commit()


def upsert_sql(table: TableSpec, columns: list[str]) -> str:
    placeholders = ", ".join(["%s"] * len(columns))
    column_sql = ", ".join(_quote(column) for column in columns)
    conflict_sql = ", ".join(_quote(column) for column in table.primary_key)
    update_columns = [column for column in columns if column not in table.primary_key]
    if update_columns:
        updates = ", ".join(f"{_quote(column)} = EXCLUDED.{_quote(column)}" for column in update_columns)
        action = f"DO UPDATE SET {updates}"
    else:
        action = "DO NOTHING"
    return (
        f"INSERT INTO {_qualified(table)} ({column_sql}) VALUES ({placeholders}) "
        f"ON CONFLICT ({conflict_sql}) {action}"
    )


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    try:
        return None if pd.isna(value) else value
    except (TypeError, ValueError):
        return value


def _normalize_value(value: Any) -> Any:
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(_json_safe(value), allow_nan=False, default=str, sort_keys=True)
    if pd.isna(value) if not isinstance(value, (dict, list, tuple)) else False:
        return None
    return value


def upsert_rows(connection: Any, table: TableSpec, rows: list[dict[str, Any]]) -> int:
    if not rows:
        return 0
    columns = list(table.columns)
    sql = upsert_sql(table, columns)
    values = [[_normalize_value(row.get(column)) for column in columns] for row in rows]
    cursor = connection.cursor()
    cursor.executemany(sql, values)
    connection.commit()
    return len(rows)


def upsert_frame(connection: Any, table: TableSpec, frame: pd.DataFrame) -> int:
    if frame.empty:
        return 0
    return upsert_rows(connection, table, frame.to_dict(orient="records"))


def utc_now() -> datetime:
    return datetime.now(timezone.utc)
