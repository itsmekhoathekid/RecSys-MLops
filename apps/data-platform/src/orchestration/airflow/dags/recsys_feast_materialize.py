from __future__ import annotations

from orchestration.airflow.spark_utils import (
    DAG,
    FEATURE_STORE_IMAGE,
    datetime,
    env_schedule,
    pod_task,
)


FEAST_ENV_EXPORTS = """
export FEAST_POSTGRES_HOST=${FEAST_POSTGRES_HOST:-feature-postgres}
export FEAST_POSTGRES_PORT=${FEAST_POSTGRES_PORT:-5432}
export FEAST_POSTGRES_DB=${FEAST_POSTGRES_DB:-feature_store}
export FEAST_POSTGRES_SCHEMA=${FEAST_POSTGRES_SCHEMA:-feature_store}
export FEAST_POSTGRES_USER=${FEAST_POSTGRES_USER:-feast}
export FEAST_POSTGRES_PASSWORD=${FEAST_POSTGRES_PASSWORD:-feast}
export FEAST_POSTGRES_SSLMODE=${FEAST_POSTGRES_SSLMODE:-disable}
export FEAST_SQL_REGISTRY_URL="$(python -m recsys_feature_store_runtime.sql_registry_state url)"
""".strip()

FEAST_MATERIALIZE_INCREMENTAL_COMMAND = f"""
{FEAST_ENV_EXPORTS}
feast -c /opt/recsys/apps/data-platform/feature-store/feature_repo \
  materialize-incremental $(date -u +%Y-%m-%dT%H:%M:%S)
""".strip()

VERIFY_REDIS_ONLINE_STORE_COMMAND = (
    f"{FEAST_ENV_EXPORTS}\npython -m validate.governance_contracts feast-online"
)


if DAG is not None:
    with DAG(
        dag_id="recsys_feast_materialize",
        start_date=datetime(2026, 1, 1),
        schedule=env_schedule(
            "FEAST_MATERIALIZE_DAG_SCHEDULE",
            "20 */2 * * *",
        ),
        catchup=False,
        max_active_runs=1,
        tags=["recsys", "feast", "materialize", "online-store"],
    ) as recsys_feast_materialize:
        materialize_incremental = pod_task(
            "feast_materialize_incremental",
            FEATURE_STORE_IMAGE,
            FEAST_MATERIALIZE_INCREMENTAL_COMMAND,
        )
        validate_online_store = pod_task(
            "verify_redis_online_store_updated",
            FEATURE_STORE_IMAGE,
            VERIFY_REDIS_ONLINE_STORE_COMMAND,
        )

        materialize_incremental >> validate_online_store
