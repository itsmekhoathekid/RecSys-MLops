from __future__ import annotations

try:
    from airflow import DAG
    from airflow.operators.bash import BashOperator
    from pendulum import datetime
except ImportError:  # pragma: no cover
    DAG = BashOperator = datetime = None


if DAG is not None:
    with DAG(
        dag_id="feast_materialization_dag",
        start_date=datetime(2026, 1, 1),
        schedule=None,
        catchup=False,
        tags=["recsys", "feast"],
    ) as dag:
        apply_repo = BashOperator(
            task_id="feast_apply",
            bash_command="uv run python feature_store/scripts/apply_feast_repo.py",
        )
        materialize = BashOperator(
            task_id="materialize_offline_to_online",
            bash_command="uv run python feature_store/scripts/materialize_offline_to_online.py",
        )
        validate = BashOperator(
            task_id="validate_feature_store",
            bash_command="uv run python feature_store/scripts/validate_feature_store.py",
        )
        apply_repo >> materialize >> validate

