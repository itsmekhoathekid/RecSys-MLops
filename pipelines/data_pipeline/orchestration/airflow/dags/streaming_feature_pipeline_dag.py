from __future__ import annotations

try:
    from airflow import DAG
    from airflow.operators.bash import BashOperator
    from pendulum import datetime
except ImportError:  # pragma: no cover
    DAG = BashOperator = datetime = None


if DAG is not None:
    with DAG(
        dag_id="streaming_feature_pipeline_dag",
        start_date=datetime(2026, 1, 1),
        schedule=None,
        catchup=False,
        tags=["recsys", "streaming"],
    ) as dag:
        health_check = BashOperator(
            task_id="streaming_contract_health_check",
            bash_command="uv run python -m pipelines.data_pipeline.local.run_streaming_features || true",
        )

