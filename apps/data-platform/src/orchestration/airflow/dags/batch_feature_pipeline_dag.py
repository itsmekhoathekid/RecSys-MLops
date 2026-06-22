from __future__ import annotations

try:
    from airflow import DAG
    from airflow.operators.bash import BashOperator
    from pendulum import datetime
except ImportError:  # pragma: no cover
    DAG = BashOperator = datetime = None


if DAG is not None:
    with DAG(
        dag_id="batch_feature_pipeline_dag",
        start_date=datetime(2026, 1, 1),
        schedule=None,
        catchup=False,
        tags=["recsys", "features"],
    ) as dag:
        run_batch_features = BashOperator(
            task_id="run_batch_features",
            bash_command="PYTHONPATH=apps/data-platform/src uv run python apps/data-platform/src/local/run_batch_features.py",
        )
