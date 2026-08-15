from __future__ import annotations

from orchestration.airflow.spark_utils import (
    DAG,
    SPARK_IMAGE,
    datetime,
    env_schedule,
    pod_task,
    spark_native_submit,
)
from metadata.governance_catalog import BRONZE_URNS, SILVER_URNS


DP2_INGEST_COMMAND = spark_native_submit(
    "dp2_ingest_bronze_to_silver_gold",
    "local:///opt/recsys/apps/data-platform/src/features/spark/dp2_silver_gold_entrypoint.py",
    "--action ingest",
)

DP2_OPTIMIZE_COMMAND = spark_native_submit(
    "dp2_optimize_silver",
    "local:///opt/recsys/apps/data-platform/src/lakehouse/optimize.py",
    "--scope silver "
    "--pipeline DP2 "
    "--strategy ${LAKEHOUSE_OPTIMIZATION_STRATEGY:-binpack} "
    "--target-file-size-mb ${LAKEHOUSE_TARGET_FILE_SIZE_MB:-128} "
    "--min-input-files ${LAKEHOUSE_COMPACTION_MIN_INPUT_FILES:-2}",
)

DP2_VALIDATE_COMMAND = spark_native_submit(
    "dp2_verify_silver_gold",
    "local:///opt/recsys/apps/data-platform/src/features/spark/dp2_silver_gold_entrypoint.py",
    "--action validate",
)


if DAG is not None:
    with DAG(
        dag_id="recsys_dp2_bronze_to_silver_gold",
        start_date=datetime(2026, 1, 1),
        schedule=env_schedule("DP2_DAG_SCHEDULE", "manual"),
        catchup=False,
        max_active_runs=1,
        tags=["recsys", "dp2", "bronze", "silver", "gold"],
    ) as recsys_dp2_bronze_to_silver_gold:
        ingest_stage = pod_task(
            "ingest_stage",
            SPARK_IMAGE,
            DP2_INGEST_COMMAND,
            inlets=BRONZE_URNS.values(),
            outlets=SILVER_URNS.values(),
        )
        optimize_stage = pod_task(
            "optimize_stage",
            SPARK_IMAGE,
            DP2_OPTIMIZE_COMMAND,
            inlets=SILVER_URNS.values(),
            outlets=SILVER_URNS.values(),
        )
        validate_stage = pod_task(
            "validate_stage",
            SPARK_IMAGE,
            DP2_VALIDATE_COMMAND,
            inlets=SILVER_URNS.values(),
        )

        ingest_stage >> optimize_stage >> validate_stage
