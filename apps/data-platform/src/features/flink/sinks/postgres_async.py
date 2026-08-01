from __future__ import annotations

import json
from typing import Any

from feature_store.postgres_offline_store import (
    FEATURE_TABLES,
    PostgresOfflineStoreConfig,
    ensure_offline_store_tables,
    insert_offline_rows_async,
)
from features.flink.operators.row_mappers import (
    build_late_event_dlq_row,
    build_postgres_item_feature_rows,
    build_postgres_user_feature_rows,
)
from features.flink.pyflink_compat import AsyncFunction
from features.flink.sinks import emit_progress
from features.flink.sinks.rate_limit import AsyncTokenBucketRateLimiter


def postgres_async_capacity(args: Any) -> int:
    """Keep in-flight requests bounded by the operator's connection pool."""
    return min(
        max(1, int(args.async_io_capacity)),
        max(1, int(args.postgres_async_pool_size)),
    )


def _postgres_config(args: Any) -> PostgresOfflineStoreConfig:
    return PostgresOfflineStoreConfig(
        host=args.feast_postgres_host,
        port=args.feast_postgres_port,
        database=args.feast_postgres_database,
        schema=args.feast_postgres_schema,
        user=args.feast_postgres_user,
        password=args.feast_postgres_password,
        sslmode=args.feast_postgres_sslmode,
    )


async def _open_pool(config: PostgresOfflineStoreConfig, args: Any):
    from psycopg.conninfo import make_conninfo
    from psycopg_pool import AsyncConnectionPool

    pool = AsyncConnectionPool(
        conninfo=make_conninfo(
            host=config.host,
            port=config.port,
            dbname=config.database,
            user=config.user,
            password=config.password,
            sslmode=config.sslmode,
        ),
        min_size=1,
        max_size=args.postgres_async_pool_size,
        open=False,
        timeout=float(args.async_io_timeout_seconds),
    )
    await pool.open(wait=True, timeout=float(args.async_io_timeout_seconds))
    return pool


class AsyncPostgresFeastOfflineWriter(AsyncFunction):
    def __init__(self, args: Any) -> None:
        self.args = args

    def open(self, runtime_context):
        self.config = _postgres_config(self.args)
        setup_conn = self.config.connect()
        try:
            ensure_offline_store_tables(setup_conn, self.config.schema, FEATURE_TABLES)
        finally:
            setup_conn.close()
        self.pool = None
        self.pool_lock = None
        self.rate_limiter = AsyncTokenBucketRateLimiter(
            self.args.postgres_sink_max_events_per_second,
            self.args.sink_rate_limit_burst_events,
        )
        self.writes = 0
        self.rate_limit_wait_seconds = 0.0

    async def async_invoke(self, update: dict[str, Any]) -> list[str]:
        import asyncio

        event = update["event"]
        rows_by_table = (
            build_postgres_user_feature_rows(update)
            if update["kind"] == "user"
            else build_postgres_item_feature_rows(update)
        )
        self.rate_limit_wait_seconds += await self.rate_limiter.acquire()
        if self.pool is None:
            if self.pool_lock is None:
                self.pool_lock = asyncio.Lock()
            async with self.pool_lock:
                if self.pool is None:
                    self.pool = await _open_pool(self.config, self.args)

        inserted = 0
        async with self.pool.connection() as conn:
            for table_name, rows in rows_by_table.items():
                inserted += await insert_offline_rows_async(
                    conn, self.config.schema, table_name, rows
                )
            await conn.commit()
        self.writes += inserted
        if (
            self.args.progress_log_events > 0
            and self.writes % self.args.progress_log_events == 0
        ):
            emit_progress(
                {
                    "status": "running",
                    "topic": self.args.topic,
                    "offline_store_sink": "postgres",
                    "postgres_rows": self.writes,
                }
            )
        return [
            json.dumps(
                {
                    "status": "postgres_feast_offline_written",
                    "event_id": event["event_id"],
                    "rows": inserted,
                    "total_rows": self.writes,
                    "rate_limit_wait_seconds": round(self.rate_limit_wait_seconds, 3),
                },
                sort_keys=True,
            )
        ]

    def timeout(self, update: dict[str, Any]) -> list[str]:
        event = update.get("event") or {}
        status = {
            "status": "postgres_feast_offline_timeout",
            "topic": self.args.topic,
            "event_id": event.get("event_id"),
        }
        emit_progress(status)
        return [json.dumps(status, sort_keys=True)]


class AsyncPostgresLateEventDlqWriter(AsyncFunction):
    def __init__(self, args: Any) -> None:
        self.args = args

    def open(self, runtime_context):
        self.config = _postgres_config(self.args)
        setup_conn = self.config.connect()
        try:
            ensure_offline_store_tables(
                setup_conn, self.config.schema, ("stream_late_events_dlq",)
            )
        finally:
            setup_conn.close()
        self.pool = None
        self.pool_lock = None
        self.writes = 0

    async def async_invoke(self, event: dict[str, Any]) -> list[str]:
        import asyncio

        if self.pool is None:
            if self.pool_lock is None:
                self.pool_lock = asyncio.Lock()
            async with self.pool_lock:
                if self.pool is None:
                    self.pool = await _open_pool(self.config, self.args)
        row = build_late_event_dlq_row(
            event, self.args.topic, self.args.allowed_lateness_seconds
        )
        async with self.pool.connection() as conn:
            inserted = await insert_offline_rows_async(
                conn, self.config.schema, "stream_late_events_dlq", [row]
            )
            await conn.commit()
        self.writes += inserted
        return [
            json.dumps(
                {
                    "status": "late_event_dlq_written",
                    "event_id": event["event_id"],
                    "late_by_seconds": float(row["late_by_seconds"]),
                    "total_rows": self.writes,
                },
                sort_keys=True,
            )
        ]

    def timeout(self, event: dict[str, Any]) -> list[str]:
        status = {
            "status": "late_event_dlq_timeout",
            "topic": self.args.topic,
            "event_id": event.get("event_id"),
        }
        emit_progress(status)
        return [json.dumps(status, sort_keys=True)]
