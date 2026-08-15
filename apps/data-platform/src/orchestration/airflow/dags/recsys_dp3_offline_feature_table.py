from __future__ import annotations

import os

from orchestration.airflow.spark_utils import (
    DAG,
    FEATURE_STORE_IMAGE,
    SPARK_IMAGE,
    datetime,
    env_schedule,
    pod_task,
    spark_native_submit,
)
from metadata.governance_catalog import (
    BRONZE_URNS,
    ICEBERG_FEATURE_URNS,
    POSTGRES_FEATURE_URNS,
    SILVER_URNS,
)


DP3_FEATURE_COMMAND = spark_native_submit(
    "dp3_offline_feature_table",
    "local:///opt/recsys/apps/data-platform/src/features/spark/dp3_offline_feature_entrypoint.py",
    "--config $DP3_CONFIG",
)
VERIFY_POSTGRES_OFFLINE_STORE_COMMAND = (
    "python -m validate.governance_contracts dp3-postgres"
)


def _enabled(name: str, default: str = "true") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


DP3_INPUT_URNS = (
    BRONZE_URNS.values()
    if os.getenv("DP3_SOURCE", "silver_lakehouse") != "silver_lakehouse"
    else SILVER_URNS.values()
)
DP3_OUTPUT_URNS = list(ICEBERG_FEATURE_URNS.values())
if _enabled("FEAST_POSTGRES_EXPORT_ENABLED"):
    DP3_OUTPUT_URNS.extend(POSTGRES_FEATURE_URNS.values())


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
            inlets=DP3_INPUT_URNS,
            outlets=DP3_OUTPUT_URNS,
        )
        validate_stage = pod_task(
            "validate_stage",
            FEATURE_STORE_IMAGE,
            VERIFY_POSTGRES_OFFLINE_STORE_COMMAND,
            inlets=POSTGRES_FEATURE_URNS.values(),
        )

        ingest_stage >> validate_stage
