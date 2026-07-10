from __future__ import annotations

import os

# Airflow DAG definitions for rubric DP1/DP2/DP3 orchestration proof.
try:
    from airflow import DAG
    from airflow.providers.cncf.kubernetes.operators.pod import KubernetesPodOperator
    from kubernetes.client import models as k8s
    from pendulum import datetime
except ImportError:  # pragma: no cover
    DAG = KubernetesPodOperator = datetime = k8s = None


NAMESPACE = "recsys-dataflow"
DATAFLOW_IMAGE = os.getenv("DATAFLOW_IMAGE", "recsys-dataflow-cli:local")
SPARK_IMAGE = os.getenv("SPARK_IMAGE", os.getenv("SPARK_K8S_IMAGE", "recsys-spark:local"))
DATAFLOW_NODE_SELECTOR = os.getenv("DATAFLOW_NODE_SELECTOR", "recsys.ai/pool=cpu-services")
COMMON_ENV = {
    "PYTHONPATH": "/opt/recsys/apps/data-platform/src:/opt/recsys",
}
SPARK_DRIVER_EXECUTOR_ENV = (
    "PYTHONPATH",
    "DATA_PLATFORM_MINIO_ENDPOINT",
    "MINIO_ENDPOINT",
    "AWS_DEFAULT_REGION",
    "LAKE_BUCKET",
    "OFFLINE_FEATURE_BUCKET",
    "LAKEHOUSE_WAREHOUSE",
    "ICEBERG_CATALOG",
    "ICEBERG_LAKEHOUSE_NAMESPACE",
    "OFFLINE_FEATURE_CATALOG",
    "OFFLINE_FEATURE_STORE_WAREHOUSE",
    "ICEBERG_FEATURE_NAMESPACE",
    "OFFLINE_FEATURE_STORE_URI",
    "FEAST_POSTGRES_HOST",
    "FEAST_POSTGRES_PORT",
    "FEAST_POSTGRES_DB",
    "FEAST_POSTGRES_SCHEMA",
    "FEAST_POSTGRES_USER",
    "FEAST_POSTGRES_PASSWORD",
    "FEAST_POSTGRES_SSLMODE",
    "FEAST_POSTGRES_EXPORT_ENABLED",
    "SPARK_SQL_SHUFFLE_PARTITIONS",
)
SPARK_SECRET_ENV = ("MINIO_ROOT_USER", "MINIO_ROOT_PASSWORD", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY")


def env_schedule(name: str, default: str | None):
    schedule = os.getenv(name, default or "")
    if schedule.lower() in {"", "none", "manual"}:
        return None
    return schedule


def pod_env_from():
    if k8s is None:
        return []
    return [
        k8s.V1EnvFromSource(config_map_ref=k8s.V1ConfigMapEnvSource(name="recsys-data-platform-config")),
        k8s.V1EnvFromSource(secret_ref=k8s.V1SecretEnvSource(name="recsys-data-platform-secret")),
    ]


def parse_node_selector(value: str) -> dict[str, str]:
    selectors: dict[str, str] = {}
    for item in (value or "").split(","):
        if not item.strip():
            continue
        key, _, raw = item.partition("=")
        if not key.strip() or not raw.strip():
            raise ValueError(f"Invalid node selector item: {item}")
        selectors[key.strip()] = raw.strip()
    return selectors


def pod_task(task_id: str, image: str, command: str):
    return KubernetesPodOperator(
        task_id=task_id,
        name=task_id.replace("_", "-"),
        namespace=NAMESPACE,
        image=image,
        cmds=["bash", "-c"],
        arguments=[f"set -euo pipefail; {command}"],
        env_vars=COMMON_ENV,
        env_from=pod_env_from(),
        annotations={"sidecar.istio.io/inject": "false"},
        node_selector=parse_node_selector(DATAFLOW_NODE_SELECTOR),
        get_logs=True,
        is_delete_operator_pod=True,
        in_cluster=True,
        startup_timeout_seconds=600,
    )


def spark_native_submit(task_id: str, application: str, application_args: str = "") -> str:
    app_name = f"recsys-{task_id.replace('_', '-')}"
    env_conf = " ".join(
        f"--conf spark.kubernetes.driverEnv.{name}=${{{name}:-}} "
        f"--conf spark.executorEnv.{name}=${{{name}:-}}"
        for name in SPARK_DRIVER_EXECUTOR_ENV
    )
    secret_conf = " ".join(
        f"--conf spark.kubernetes.driver.secretKeyRef.{name}=recsys-data-platform-secret:{name} "
        f"--conf spark.kubernetes.executor.secretKeyRef.{name}=recsys-data-platform-secret:{name}"
        for name in SPARK_SECRET_ENV
    )
    return (
        'SPARK_APP_SUFFIX="$(date +%s)-${RANDOM}"; '
        "/opt/spark/bin/spark-submit "
        "--master ${SPARK_K8S_MASTER:-k8s://https://kubernetes.default.svc} "
        "--deploy-mode cluster "
        f"--name {app_name}-${{SPARK_APP_SUFFIX}} "
        "--conf spark.kubernetes.namespace=${SPARK_K8S_NAMESPACE:-recsys-dataflow} "
        "--conf spark.kubernetes.container.image=${SPARK_K8S_IMAGE:-recsys-spark:local} "
        "--conf spark.kubernetes.container.image.pullPolicy=${SPARK_K8S_IMAGE_PULL_POLICY:-IfNotPresent} "
        "--conf spark.kubernetes.authenticate.driver.serviceAccountName=${SPARK_K8S_SERVICE_ACCOUNT:-default} "
        "--conf spark.kubernetes.submission.waitAppCompletion=true "
        "--conf spark.kubernetes.submission.connectionTimeout=${SPARK_K8S_CONNECTION_TIMEOUT:-60000} "
        "--conf spark.kubernetes.submission.requestTimeout=${SPARK_K8S_REQUEST_TIMEOUT:-180000} "
        "--conf spark.kubernetes.report.interval=5s "
        "--conf spark.kubernetes.driver.annotation.sidecar.istio.io/inject=false "
        "--conf spark.kubernetes.executor.annotation.sidecar.istio.io/inject=false "
        "--conf spark.kubernetes.node.selector.recsys.ai/pool=${SPARK_K8S_NODE_POOL:-cpu-services} "
        "--conf spark.driver.memory=${SPARK_K8S_DRIVER_MEMORY:-1g} "
        "--conf spark.driver.memoryOverhead=${SPARK_K8S_DRIVER_MEMORY_OVERHEAD:-384m} "
        "--conf spark.driver.cores=${SPARK_K8S_DRIVER_CORES:-1} "
        "--conf spark.kubernetes.driver.request.cores=${SPARK_K8S_DRIVER_REQUEST_CORES:-500m} "
        "--conf spark.executor.instances=${SPARK_K8S_EXECUTOR_INSTANCES:-1} "
        "--conf spark.executor.memory=${SPARK_K8S_EXECUTOR_MEMORY:-1g} "
        "--conf spark.executor.memoryOverhead=${SPARK_K8S_EXECUTOR_MEMORY_OVERHEAD:-384m} "
        "--conf spark.executor.cores=${SPARK_K8S_EXECUTOR_CORES:-1} "
        "--conf spark.kubernetes.executor.request.cores=${SPARK_K8S_EXECUTOR_REQUEST_CORES:-500m} "
        f"{env_conf} "
        f"{secret_conf} "
        f"{application} {application_args}".strip()
    )


SPARK_BATCH_COMMAND = spark_native_submit(
    "dp3_offline_feature_table",
    "local:///opt/recsys/apps/data-platform/src/features/spark/spark_batch_entrypoint.py",
    "--config $SPARK_BATCH_CONFIG",
)

VERIFY_POSTGRES_OFFLINE_STORE_COMMAND = r"""
python -c '
import json
from psycopg import sql
from feature_store.postgres_offline_store import OFFLINE_STORE_TABLES, PostgresOfflineStoreConfig

config = PostgresOfflineStoreConfig.from_env()
counts = {}
with config.connect() as conn:
    with conn.cursor() as cur:
        for table_name in OFFLINE_STORE_TABLES:
            cur.execute(
                sql.SQL("SELECT COUNT(*) FROM {}.{}").format(
                    sql.Identifier(config.schema),
                    sql.Identifier(table_name),
                )
            )
            counts[table_name] = int(cur.fetchone()[0])
missing = {name: count for name, count in counts.items() if count <= 0}
assert not missing, f"PostgreSQL Feast offline tables are empty: {missing}; counts={counts}"
print(json.dumps({"postgres_feast_offline_store_counts": counts}, sort_keys=True))
'
""".strip()


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
print(json.dumps({"dp1_bronze_table_counts": counts}, sort_keys=True))
PY
""".strip()

DP2_INGEST_COMMAND = spark_native_submit(
    "dp2_ingest_bronze_to_silver_gold",
    "local:///opt/recsys/apps/data-platform/src/features/spark/dp2_silver_gold_entrypoint.py",
    "--action ingest",
)

DP2_VALIDATE_COMMAND = spark_native_submit(
    "dp2_verify_silver_gold",
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
        )
        validate_stage = pod_task(
            "validate_stage",
            DATAFLOW_IMAGE,
            DP1_VALIDATE_COMMAND,
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
        )
        validate_stage = pod_task(
            "validate_stage",
            SPARK_IMAGE,
            DP2_VALIDATE_COMMAND,
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
        )
        validate_stage = pod_task(
            "validate_stage",
            DATAFLOW_IMAGE,
            VERIFY_POSTGRES_OFFLINE_STORE_COMMAND,
        )

        ingest_stage >> validate_stage
