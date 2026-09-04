from __future__ import annotations

from orchestration.airflow.spark_utils import (
    DAG,
    DATAHUB_OPS_IMAGE,
    SPARK_DATA_IMAGE,
    datahub_validation_command,
    datetime,
    env_schedule,
    pod_task,
    spark_native_submit,
)

REPORT_URI = "s3://recsys-lakehouse/governance-validation/DP2/{{ ts_nodash }}/dp2.json"
DP2_DATASET_KEYS = tuple(
    f"silver.{table}"
    for table in (
        "clean_behavior_events",
        "rejected_behavior_events",
        "clean_impressions",
        "clean_recommendation_requests",
        "product_scd",
        "users",
        "products",
        "user_preferences",
    )
)


DP2_INGEST_COMMAND = spark_native_submit(
    "dp2_ingest_bronze_to_silver_gold",
    "local:///opt/recsys/apps/data-platform/src/features/spark/dp2_silver_gold_entrypoint.py",
    "--action ingest",
)

DP2_OPTIMIZE_COMMAND = spark_native_submit(
    "dp2_optimize_silver",
    "local:///opt/recsys/apps/data-platform/src/lakehouse/optimize.py",
    "--scope silver "
    "--strategy ${LAKEHOUSE_OPTIMIZATION_STRATEGY:-binpack} "
    "--target-file-size-mb ${LAKEHOUSE_TARGET_FILE_SIZE_MB:-128} "
    "--min-input-files ${LAKEHOUSE_COMPACTION_MIN_INPUT_FILES:-2}",
)

DP2_VALIDATE_COMMAND = spark_native_submit(
    "dp2_verify_silver_gold",
    "local:///opt/recsys/apps/data-platform/src/features/spark/dp2_silver_gold_entrypoint.py",
    f"--action validate --report-uri '{REPORT_URI}' --run-id '{{{{ run_id }}}}'",
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
            SPARK_DATA_IMAGE,
            DP2_INGEST_COMMAND,
        )
        optimize_stage = pod_task(
            "optimize_stage",
            SPARK_DATA_IMAGE,
            DP2_OPTIMIZE_COMMAND,
        )
        validate_stage = pod_task(
            "validate_stage",
            SPARK_DATA_IMAGE,
            DP2_VALIDATE_COMMAND,
        )
        publish_datahub_validation = pod_task(
            "publish_datahub_validation",
            DATAHUB_OPS_IMAGE,
            datahub_validation_command("DP2", (REPORT_URI,), DP2_DATASET_KEYS),
            trigger_rule="all_success",
            retries=2,
        )

        ingest_stage >> optimize_stage >> validate_stage >> publish_datahub_validation
