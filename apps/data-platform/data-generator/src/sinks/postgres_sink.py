from __future__ import annotations

from pathlib import Path
from typing import Any

from sink import read_table


DEFAULT_TABLE_ORDER = [
    "users",
    "products",
    "user_preferences",
    "product_snapshots",
    "sessions",
    "recommendation_requests",
    "impressions",
    "behavior_events",
    "orders",
    "order_items",
]

PRIMARY_KEYS = {
    "users": ["user_id"],
    "user_preferences": ["user_id", "category_id", "brand_id"],
    "products": ["product_id"],
    "product_snapshots": ["product_id", "valid_from"],
    "sessions": ["session_id"],
    "recommendation_requests": ["request_id"],
    "impressions": ["impression_id"],
    "behavior_events": ["event_id", "payload_hash"],
    "orders": ["order_id"],
    "order_items": ["order_item_id"],
}


def build_upsert_sql(table_name: str, columns: list[str]) -> str:
    placeholders = ", ".join(["%s"] * len(columns))
    column_sql = ", ".join(columns)
    pk = PRIMARY_KEYS[table_name]
    updates = [column for column in columns if column not in pk]
    if updates:
        update_sql = ", ".join(f"{column} = EXCLUDED.{column}" for column in updates)
        conflict_sql = f"DO UPDATE SET {update_sql}"
    else:
        conflict_sql = "DO NOTHING"
    return (
        f"INSERT INTO {table_name} ({column_sql}) VALUES ({placeholders}) "
        f"ON CONFLICT ({', '.join(pk)}) {conflict_sql}"
    )


def normalize_postgres_row(table_name: str, row: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(row)
    if table_name == "user_preferences" and normalized.get("brand_id") is None:
        normalized["brand_id"] = 0
    return normalized


def load_run_to_postgres(
    run_path: str | Path,
    connection: Any,
    tables: list[str] | None = None,
    limit_per_table: int | None = None,
) -> dict[str, int]:
    """Load generated Parquet tables into PostgreSQL using a DB-API connection.

    The function is intentionally DB-API based so psycopg can be supplied by the
    Docker/Airflow runtime without becoming a hard dependency for local tests.
    """
    counts: dict[str, int] = {}
    cursor = connection.cursor()
    for table_name in tables or DEFAULT_TABLE_ORDER:
        table = read_table(Path(run_path), table_name)
        rows = [normalize_postgres_row(table_name, row) for row in table.to_pylist()]
        if limit_per_table is not None:
            rows = rows[:limit_per_table]
        if not rows:
            counts[table_name] = 0
            continue
        columns = list(rows[0].keys())
        sql = build_upsert_sql(table_name, columns)
        cursor.executemany(sql, [[row[column] for column in columns] for row in rows])
        counts[table_name] = len(rows)
    connection.commit()
    return counts
