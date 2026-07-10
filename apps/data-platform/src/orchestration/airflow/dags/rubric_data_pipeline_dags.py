from __future__ import annotations

from k8s_data_platform_dag import (
    DAG,
    DATAFLOW_IMAGE,
    SPARK_BATCH_COMMAND,
    SPARK_IMAGE,
    VERIFY_POSTGRES_OFFLINE_STORE_COMMAND,
    datetime,
    env_schedule,
    pod_task,
    spark_native_submit,
)


DP1_INGEST_COMMAND = """
PYTHONPATH=/opt/recsys/apps/data-platform/data-generator/src:/opt/recsys \
python apps/data-platform/data-generator/src/scripts/generate_historical_to_minio.py \
  --config $DATA_GENERATOR_CONFIG \
  --target s3 \
  --bucket $LAKE_BUCKET \
  --prefix raw

python -m ingest.batch_lakehouse_ingestion \
  --run-path s3a://$LAKE_BUCKET/raw/$DATA_GENERATOR_RUN_ID \
  --lakehouse-warehouse $LAKEHOUSE_WAREHOUSE \
  --mode overwrite
""".strip()

DP1_VALIDATE_COMMAND = r"""
python - <<'PY'
import json
import os

import pyarrow.parquet as pq

from ingest.batch_lakehouse_ingestion import _filesystem_and_path
from lakehouse.iceberg import RAW_GENERATOR_TABLES

base = os.getenv("LAKEHOUSE_WAREHOUSE", "s3a://recsys-lakehouse/warehouse").rstrip("/")
namespace = os.getenv("ICEBERG_LAKEHOUSE_NAMESPACE", "lakehouse")
counts = {}
for table_name in RAW_GENERATOR_TABLES:
    table_uri = f"{base}/{namespace}/{table_name}"
    filesystem, path = _filesystem_and_path(table_uri)
    table = pq.read_table(path, filesystem=filesystem)
    counts[table_name] = table.num_rows
missing = {name: count for name, count in counts.items() if count <= 0}
assert not missing, f"DP1 bronze lakehouse tables are empty: {missing}; counts={counts}"
print(json.dumps({"dp1_validate_bronze_counts": counts}, sort_keys=True))
PY
""".strip()

DP2_INGEST_COMMAND = spark_native_submit(
    "dp2_ingest_bronze_to_silver_gold",
    "local:///opt/recsys/apps/data-platform/src/features/spark/dp2_silver_gold_entrypoint.py",
    "--action ingest",
)

DP2_VALIDATE_COMMAND = spark_native_submit(
    "dp2_validate_bronze_to_silver_gold",
    "local:///opt/recsys/apps/data-platform/src/features/spark/dp2_silver_gold_entrypoint.py",
    "--action validate",
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
            DATAFLOW_IMAGE,
            DP1_INGEST_COMMAND,
            mesh=False,
        )
        validate_stage = pod_task(
            "validate_stage",
            DATAFLOW_IMAGE,
            DP1_VALIDATE_COMMAND,
            mesh=False,
        )

        ingest_stage >> validate_stage

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
            mesh=False,
        )
        validate_stage = pod_task(
            "validate_stage",
            SPARK_IMAGE,
            DP2_VALIDATE_COMMAND,
            mesh=False,
        )

        ingest_stage >> validate_stage

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
            SPARK_BATCH_COMMAND,
            mesh=False,
        )
        validate_stage = pod_task(
            "validate_stage",
            DATAFLOW_IMAGE,
            VERIFY_POSTGRES_OFFLINE_STORE_COMMAND,
        )

        ingest_stage >> validate_stage
