from __future__ import annotations

from orchestration.airflow.spark_utils import (
    DAG,
    FEATURE_STORE_IMAGE,
    SPARK_IMAGE,
    datetime,
    env_schedule,
    pod_task,
    spark_native_submit,
)


DP3_FEATURE_COMMAND = spark_native_submit(
    "dp3_offline_feature_table",
    "local:///opt/recsys/apps/data-platform/src/features/spark/dp3_offline_feature_entrypoint.py",
    "--config $DP3_CONFIG",
)
VERIFY_POSTGRES_OFFLINE_STORE_COMMAND = (
    "python -m validate.governance_contracts dp3-postgres"
)


if DAG is not None:
    with DAG(
        dag_id="recsys_dp3_offline_feature_table",
        start_date=datetime(2026, 1, 1),
        schedule=env_schedule("DP3_DAG_SCHEDULE", "manual"),
        catchup=False,
        max_active_runs=1,
        tags=["recsys", "dp3", "offline-store", "features"],
    ) as recsys_dp3_offline_feature_table:
        ingest_stage = pod_task(
            "ingest_stage",
            SPARK_IMAGE,
            DP3_FEATURE_COMMAND,
        )
        validate_stage = pod_task(
            "validate_stage",
            FEATURE_STORE_IMAGE,
            VERIFY_POSTGRES_OFFLINE_STORE_COMMAND,
        )

        ingest_stage >> validate_stage
