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
    "VALIDATION_RUN_ID": "{{ run_id }}",
    "RUNTIME_LINEAGE_ENABLED": "true",
    "RUNTIME_LINEAGE_STRICT": "true",
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
    "SPARK_ADVISORY_PARTITION_SIZE_BYTES",
    "GOVERNANCE_VALIDATION_ROOT",
    "RUNTIME_LINEAGE_ROOT",
    "RUNTIME_LINEAGE_ENABLED",
    "RUNTIME_LINEAGE_STRICT",
    "VALIDATION_RUN_ID",
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
        "--conf spark.dynamicAllocation.enabled=${SPARK_DYNAMIC_ALLOCATION_ENABLED:-false} "
        "--conf spark.dynamicAllocation.shuffleTracking.enabled=${SPARK_DYNAMIC_ALLOCATION_SHUFFLE_TRACKING_ENABLED:-true} "
        "--conf spark.dynamicAllocation.minExecutors=${SPARK_DYNAMIC_ALLOCATION_MIN_EXECUTORS:-1} "
        "--conf spark.dynamicAllocation.initialExecutors=${SPARK_DYNAMIC_ALLOCATION_INITIAL_EXECUTORS:-1} "
        "--conf spark.dynamicAllocation.maxExecutors=${SPARK_DYNAMIC_ALLOCATION_MAX_EXECUTORS:-4} "
        "--conf spark.dynamicAllocation.executorIdleTimeout=${SPARK_DYNAMIC_ALLOCATION_EXECUTOR_IDLE_TIMEOUT:-60s} "
        "--conf spark.dynamicAllocation.schedulerBacklogTimeout=${SPARK_DYNAMIC_ALLOCATION_SCHEDULER_BACKLOG_TIMEOUT:-1s} "
        "--conf spark.dynamicAllocation.sustainedSchedulerBacklogTimeout=${SPARK_DYNAMIC_ALLOCATION_SUSTAINED_BACKLOG_TIMEOUT:-1s} "
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

VERIFY_POSTGRES_OFFLINE_STORE_COMMAND = "python -m validate.governance_contracts dp3-postgres"


DP1_INGEST_COMMAND = """
PYTHONPATH=/opt/recsys/apps/data-platform/data-generator/src:/opt/recsys/apps/data-platform/src:/opt/recsys \
python apps/data-platform/data-generator/src/cli.py generate \
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
    "--pipeline DP1 "
    "--strategy ${LAKEHOUSE_OPTIMIZATION_STRATEGY:-binpack} "
    "--target-file-size-mb ${LAKEHOUSE_TARGET_FILE_SIZE_MB:-128} "
    "--min-input-files ${LAKEHOUSE_COMPACTION_MIN_INPUT_FILES:-2}",
)

DP1_VALIDATE_COMMAND = spark_native_submit(
    "dp1_validate_iceberg",
    "local:///opt/recsys/apps/data-platform/src/validate/governance_contracts.py",
    "dp1",
)

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

DP2_OPTIMIZE_COMMAND = spark_native_submit(
    "dp2_optimize_silver",
    "local:///opt/recsys/apps/data-platform/src/lakehouse/optimize.py",
    "--scope silver "
    "--pipeline DP2 "
    "--strategy ${LAKEHOUSE_OPTIMIZATION_STRATEGY:-binpack} "
    "--target-file-size-mb ${LAKEHOUSE_TARGET_FILE_SIZE_MB:-128} "
    "--min-input-files ${LAKEHOUSE_COMPACTION_MIN_INPUT_FILES:-2}",
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
        optimize_stage = pod_task(
            "optimize_stage",
            SPARK_IMAGE,
            DP2_OPTIMIZE_COMMAND,
        )
        validate_stage = pod_task(
            "validate_stage",
            SPARK_IMAGE,
            DP2_VALIDATE_COMMAND,
        )

        ingest_stage >> optimize_stage >> validate_stage

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
