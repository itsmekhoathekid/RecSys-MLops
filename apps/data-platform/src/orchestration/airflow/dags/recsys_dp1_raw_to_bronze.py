from __future__ import annotations

from orchestration.airflow.spark_utils import (
    DAG,
    SPARK_IMAGE,
    datetime,
    env_schedule,
    pod_task,
    spark_native_submit,
)


DP1_INGEST_COMMAND = """
PYTHONPATH=/opt/recsys/apps/data-platform/data-generator/src:/opt/recsys/apps/data-platform/src:/opt/recsys/packages/recsys-feature-store-runtime/src:/opt/recsys \
python3 apps/data-platform/data-generator/src/cli.py generate \
  --config $DATA_GENERATOR_CONFIG

/opt/spark/bin/spark-submit \
  --master local[*] \
  --deploy-mode client \
  --name recsys-dp1-generator-to-iceberg \
  /opt/recsys/apps/data-platform/src/ingest/batch_lakehouse_ingestion.py \
  --run-path apps/data-platform/data-generator/src/output/$DATA_GENERATOR_RUN_ID \
  --run-id $DATA_GENERATOR_RUN_ID \
  --lakehouse-warehouse $LAKEHOUSE_WAREHOUSE \
  --mode overwrite
""".strip()

DP1_OPTIMIZE_COMMAND = spark_native_submit(
    "dp1_optimize_bronze",
    "local:///opt/recsys/apps/data-platform/src/lakehouse/optimize.py",
    "--scope bronze "
    "--strategy ${LAKEHOUSE_OPTIMIZATION_STRATEGY:-binpack} "
    "--target-file-size-mb ${LAKEHOUSE_TARGET_FILE_SIZE_MB:-128} "
    "--min-input-files ${LAKEHOUSE_COMPACTION_MIN_INPUT_FILES:-2}",
)

DP1_VALIDATE_COMMAND = spark_native_submit(
    "dp1_validate_iceberg",
    "local:///opt/recsys/apps/data-platform/src/validate/governance_contracts.py",
    "dp1",
)


if DAG is not None:
    with DAG(
        dag_id="recsys_dp1_raw_to_bronze",
        start_date=datetime(2026, 1, 1),
        schedule=env_schedule("DP1_DAG_SCHEDULE", "manual"),
        catchup=False,
        max_active_runs=1,
        tags=["recsys", "dp1", "raw", "bronze"],
    ) as recsys_dp1_raw_to_bronze:
        ingest_stage = pod_task(
            "ingest_stage",
            SPARK_IMAGE,
            DP1_INGEST_COMMAND,
        )
        optimize_stage = pod_task(
            "optimize_stage",
            SPARK_IMAGE,
            DP1_OPTIMIZE_COMMAND,
        )
        validate_stage = pod_task(
            "validate_stage",
            SPARK_IMAGE,
            DP1_VALIDATE_COMMAND,
        )

        ingest_stage >> optimize_stage >> validate_stage
