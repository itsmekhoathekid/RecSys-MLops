from __future__ import annotations

try:
    from airflow import DAG
    from airflow.operators.bash import BashOperator
    from pendulum import datetime
except ImportError:  # pragma: no cover
    DAG = BashOperator = datetime = None


if DAG is not None:
    with DAG(
        dag_id="raw_ingestion_dag",
        start_date=datetime(2026, 1, 1),
        schedule=None,
        catchup=False,
        tags=["recsys", "data-platform"],
    ) as dag:
        generate_historical = BashOperator(
            task_id="generate_historical_to_minio_layout",
            bash_command="PYTHONPATH=apps/data-platform/data-generator/src uv run python apps/data-platform/data-generator/src/scripts/generate_historical_to_minio.py",
        )
