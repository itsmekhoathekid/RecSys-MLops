from __future__ import annotations

import math
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable

import psycopg
from psycopg import sql


FEATURE_TABLES = (
    "user_sequence_features",
    "user_aggregate_features",
    "item_features",
)

OFFLINE_STORE_TABLES = FEATURE_TABLES + ("ml_ranking_labels",)

TABLE_SCHEMAS: dict[str, list[tuple[str, str]]] = {
    "user_sequence_features": [
        ("user_id", "BIGINT"),
        ("feature_timestamp", "TIMESTAMPTZ"),
        ("event_timestamp", "TIMESTAMPTZ"),
        ("created_timestamp", "TIMESTAMPTZ"),
        ("hist_item_ids", "BIGINT[]"),
        ("hist_event_type_ids", "BIGINT[]"),
        ("hist_category_ids", "BIGINT[]"),
        ("hist_brand_ids", "BIGINT[]"),
        ("hist_price_bucket_ids", "BIGINT[]"),
        ("hist_event_timestamps", "TEXT[]"),
        ("hist_request_ids", "TEXT[]"),
        ("hist_impression_ids", "TEXT[]"),
        ("hist_length", "BIGINT"),
        ("max_history_length", "BIGINT"),
        ("feature_version", "TEXT"),
    ],
    "user_aggregate_features": [
        ("user_id", "BIGINT"),
        ("feature_timestamp", "TIMESTAMPTZ"),
        ("event_timestamp", "TIMESTAMPTZ"),
        ("views_30m", "BIGINT"),
        ("carts_30m", "BIGINT"),
        ("purchases_24h", "BIGINT"),
        ("distinct_categories_7d", "BIGINT"),
        ("avg_viewed_price_7d", "DOUBLE PRECISION"),
        ("cart_to_purchase_ratio_7d", "DOUBLE PRECISION"),
        ("last_event_age_seconds", "BIGINT"),
        ("aggregation_window_end_ts", "TIMESTAMPTZ"),
        ("watermark_ts", "TIMESTAMPTZ"),
        ("created_timestamp", "TIMESTAMPTZ"),
        ("feature_version", "TEXT"),
    ],
    "item_features": [
        ("product_id", "BIGINT"),
        ("feature_timestamp", "TIMESTAMPTZ"),
        ("event_timestamp", "TIMESTAMPTZ"),
        ("category_id", "BIGINT"),
        ("brand_id", "BIGINT"),
        ("price_bucket", "BIGINT"),
        ("is_active", "BOOLEAN"),
        ("views_1h", "BIGINT"),
        ("views_24h", "BIGINT"),
        ("carts_1h", "BIGINT"),
        ("carts_24h", "BIGINT"),
        ("purchases_24h", "BIGINT"),
        ("purchases_7d", "BIGINT"),
        ("conversion_rate_7d", "DOUBLE PRECISION"),
        ("popularity_score", "DOUBLE PRECISION"),
        ("aggregation_window_end_ts", "TIMESTAMPTZ"),
        ("watermark_ts", "TIMESTAMPTZ"),
        ("created_timestamp", "TIMESTAMPTZ"),
        ("feature_version", "TEXT"),
    ],
    "ml_ranking_labels": [
        ("impression_id", "TEXT"),
        ("request_id", "TEXT"),
        ("user_id", "BIGINT"),
        ("candidate_product_id", "BIGINT"),
        ("prediction_timestamp", "TIMESTAMPTZ"),
        ("label_window_end", "TIMESTAMPTZ"),
        ("label", "BIGINT"),
        ("positive_event_type", "TEXT"),
        ("positive_event_timestamp", "TIMESTAMPTZ"),
        ("sampling_strategy", "TEXT"),
        ("sampling_probability", "DOUBLE PRECISION"),
        ("candidate_source", "TEXT"),
        ("rank_position", "BIGINT"),
        ("created_timestamp", "TIMESTAMPTZ"),
        ("label_version", "TEXT"),
    ],
    "stream_late_events_dlq": [
        ("event_id", "TEXT"),
        ("user_id", "BIGINT"),
        ("product_id", "BIGINT"),
        ("event_type", "TEXT"),
        ("event_timestamp", "TIMESTAMPTZ"),
        ("processed_timestamp", "TIMESTAMPTZ"),
        ("late_by_seconds", "DOUBLE PRECISION"),
        ("allowed_lateness_seconds", "BIGINT"),
        ("source_topic", "TEXT"),
        ("payload_hash", "TEXT"),
        ("reason", "TEXT"),
        ("payload", "TEXT"),
        ("created_timestamp", "TIMESTAMPTZ"),
    ],
}


@dataclass(frozen=True)
class PostgresOfflineStoreConfig:
    host: str
    port: int
    database: str
    schema: str
    user: str
    password: str
    sslmode: str = "disable"

    @classmethod
    def from_env(cls) -> "PostgresOfflineStoreConfig":
        return cls(
            host=os.getenv("FEAST_POSTGRES_HOST", "feature-postgres"),
            port=int(os.getenv("FEAST_POSTGRES_PORT", "5432")),
            database=os.getenv("FEAST_POSTGRES_DB", "feature_store"),
            schema=os.getenv("FEAST_POSTGRES_SCHEMA", "feature_store"),
            user=os.getenv("FEAST_POSTGRES_USER", "feast"),
            password=os.getenv("FEAST_POSTGRES_PASSWORD", "feast"),
            sslmode=os.getenv("FEAST_POSTGRES_SSLMODE", "disable"),
        )

    @classmethod
    def from_output(cls, output: dict[str, Any]) -> "PostgresOfflineStoreConfig":
        config = output.get("feast_postgres_export", {})
        return cls(
            host=config.get("host") or output.get("feast_postgres_host") or os.getenv("FEAST_POSTGRES_HOST", "feature-postgres"),
            port=int(config.get("port") or output.get("feast_postgres_port") or os.getenv("FEAST_POSTGRES_PORT", "5432")),
            database=config.get("database") or output.get("feast_postgres_db") or os.getenv("FEAST_POSTGRES_DB", "feature_store"),
            schema=config.get("schema") or output.get("feast_postgres_schema") or os.getenv("FEAST_POSTGRES_SCHEMA", "feature_store"),
            user=config.get("user") or output.get("feast_postgres_user") or os.getenv("FEAST_POSTGRES_USER", "feast"),
            password=config.get("password") or output.get("feast_postgres_password") or os.getenv("FEAST_POSTGRES_PASSWORD", "feast"),
            sslmode=config.get("sslmode") or output.get("feast_postgres_sslmode") or os.getenv("FEAST_POSTGRES_SSLMODE", "disable"),
        )

    def connect(self) -> psycopg.Connection:
        return psycopg.connect(
            host=self.host,
            port=self.port,
            dbname=self.database,
            user=self.user,
            password=self.password,
            sslmode=self.sslmode,
        )


def _column_defs(table_name: str) -> list[tuple[str, str]]:
    try:
        return TABLE_SCHEMAS[table_name]
    except KeyError as exc:
        raise ValueError(f"Unsupported Feast PostgreSQL offline table: {table_name}") from exc


def ensure_offline_store_tables(conn: psycopg.Connection, schema: str, tables: Iterable[str] = OFFLINE_STORE_TABLES) -> None:
    with conn.cursor() as cur:
        cur.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(schema)))
        for table_name in tables:
            columns = sql.SQL(", ").join(
                sql.SQL("{} {}").format(sql.Identifier(name), sql.SQL(pg_type))
                for name, pg_type in _column_defs(table_name)
            )
            cur.execute(
                sql.SQL("CREATE TABLE IF NOT EXISTS {}.{} ({})").format(
                    sql.Identifier(schema),
                    sql.Identifier(table_name),
                    columns,
                )
            )
        conn.commit()


def truncate_offline_store_tables(conn: psycopg.Connection, schema: str, tables: Iterable[str]) -> None:
    table_identifiers = [
        sql.SQL("{}.{}").format(sql.Identifier(schema), sql.Identifier(table_name))
        for table_name in tables
    ]
    if not table_identifiers:
        return
    with conn.cursor() as cur:
        cur.execute(sql.SQL("TRUNCATE TABLE {}").format(sql.SQL(", ").join(table_identifiers)))
        conn.commit()


def insert_offline_rows(conn: psycopg.Connection, schema: str, table_name: str, rows: list[dict[str, Any]]) -> int:
    if not rows:
        return 0
    known_columns = [name for name, _ in _column_defs(table_name)]
    columns = [column for column in known_columns if column in rows[0]]
    placeholders = sql.SQL(", ").join(sql.Placeholder() for _ in columns)
    statement = sql.SQL("INSERT INTO {}.{} ({}) VALUES ({})").format(
        sql.Identifier(schema),
        sql.Identifier(table_name),
        sql.SQL(", ").join(sql.Identifier(column) for column in columns),
        placeholders,
    )
    values = [[_coerce_value(row.get(column)) for column in columns] for row in rows]
    with conn.cursor() as cur:
        cur.executemany(statement, values)
        conn.commit()
    return len(rows)


def _coerce_value(value: Any) -> Any:
    if value is None:
        return None
    try:
        import pandas as pd

        if pd.isna(value):
            return None
    except (ImportError, TypeError, ValueError):
        pass
    if isinstance(value, list):
        return [_coerce_value(item) for item in value]
    if isinstance(value, tuple):
        return [_coerce_value(item) for item in value]
    if isinstance(value, float) and math.isnan(value):
        return None
    if hasattr(value, "to_pydatetime"):
        return value.to_pydatetime()
    if hasattr(value, "item"):
        return _coerce_value(value.item())
    if isinstance(value, datetime):
        return value
    return value
