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
        ingest_batch_to_lakehouse = BashOperator(
            task_id="ingest_batch_to_lakehouse",
            bash_command=(
                "PYTHONPATH=apps/data-platform/src spark-submit "
                "apps/data-platform/src/ingest/batch_lakehouse_ingestion.py "
                "--run-path apps/data-platform/data-generator/src/output/test_10k_seed42 "
                "--mode overwrite"
            ),
        )
        run_batch_features = BashOperator(
            task_id="run_batch_features",
            bash_command=(
                "PYTHONPATH=apps/data-platform/src spark-submit "
                "apps/data-platform/src/feature_engineering/spark/spark_batch_entrypoint.py"
            ),
        )
        ingest_batch_to_lakehouse >> run_batch_features
