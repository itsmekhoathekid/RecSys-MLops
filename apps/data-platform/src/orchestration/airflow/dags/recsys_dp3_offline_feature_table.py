from __future__ import annotations

from orchestration.airflow.spark_utils import (
    DAG,
    DATA_INGESTION_IMAGE,
    FEATURE_STORE_IMAGE,
    SPARK_IMAGE,
    datahub_validation_command,
    datetime,
    env_schedule,
    pod_task,
    spark_native_submit,
)

ICEBERG_REPORT_URI = (
    "s3://recsys-lakehouse/governance-validation/DP3/{{ ts_nodash }}/iceberg.json"
)
POSTGRES_REPORT_URI = (
    "s3://recsys-lakehouse/governance-validation/DP3/{{ ts_nodash }}/postgres.json"
)
DP3_OFFLINE_DATASET_KEYS = tuple(
    f"iceberg.feature_store.{table}"
    for table in (
        "user_sequence_features",
        "user_aggregate_features",
        "item_features",
        "ml_ranking_labels",
        "ml_bst_training",
    )
) + tuple(
    f"postgres.feature_store.{table}"
    for table in (
        "user_sequence_features",
        "user_aggregate_features",
        "item_features",
        "ml_ranking_labels",
    )
)


DP3_FEATURE_COMMAND = spark_native_submit(
    "dp3_offline_feature_table",
    "local:///opt/recsys/apps/data-platform/src/features/spark/dp3_offline_feature_entrypoint.py",
    f"--config $DP3_CONFIG --report-uri '{ICEBERG_REPORT_URI}' --run-id '{{{{ run_id }}}}'",
)
VERIFY_POSTGRES_OFFLINE_STORE_COMMAND = (
    f"python -m validate.governance_contracts dp3-postgres "
    f"--report-uri '{POSTGRES_REPORT_URI}' --run-id '{{{{ run_id }}}}'"
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
        publish_datahub_validation = pod_task(
            "publish_datahub_validation",
            DATA_INGESTION_IMAGE,
            datahub_validation_command(
                "DP3",
                (ICEBERG_REPORT_URI, POSTGRES_REPORT_URI),
                DP3_OFFLINE_DATASET_KEYS,
            ),
            trigger_rule="all_done",
        )

        ingest_stage >> validate_stage >> publish_datahub_validation
