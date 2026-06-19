from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any


def register_model_config(
    postgres_uri: str,
    model_name: str,
    model_version: str,
    artifact_uri: str,
    mlflow_run_id: str | None,
    metrics: dict[str, Any],
    config: dict[str, Any],
) -> None:
    import psycopg

    with psycopg.connect(postgres_uri) as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS model_configs (
                    id BIGSERIAL PRIMARY KEY,
                    model_name TEXT NOT NULL,
                    model_version TEXT NOT NULL,
                    artifact_uri TEXT NOT NULL,
                    mlflow_run_id TEXT,
                    metrics JSONB NOT NULL,
                    config JSONB NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL
                )
                """
            )
            cursor.execute(
                """
                INSERT INTO model_configs (
                    model_name,
                    model_version,
                    artifact_uri,
                    mlflow_run_id,
                    metrics,
                    config,
                    created_at
                )
                VALUES (%s, %s, %s, %s, %s::jsonb, %s::jsonb, %s)
                """,
                (
                    model_name,
                    model_version,
                    artifact_uri,
                    mlflow_run_id,
                    json.dumps(metrics),
                    json.dumps(config),
                    datetime.now(timezone.utc),
                ),
            )
        conn.commit()

