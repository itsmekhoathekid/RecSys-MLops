from __future__ import annotations

import os

try:
    from airflow import DAG
    from airflow.providers.cncf.kubernetes.operators.pod import KubernetesPodOperator
    from kubernetes.client import models as k8s
    from pendulum import datetime
except ImportError:  # pragma: no cover
    DAG = KubernetesPodOperator = datetime = k8s = None

NAMESPACE = "recsys-dataflow"
FEATURE_STORE_IMAGE = os.getenv(
    "FEATURE_STORE_IMAGE",
    "registry.example.invalid/recsys/recsys-feature-store:required",
)
DATA_INGESTION_IMAGE = os.getenv(
    "DATA_INGESTION_IMAGE",
    "registry.example.invalid/recsys/recsys-data-ingestion:required",
)
SPARK_IMAGE = os.getenv(
    "SPARK_IMAGE",
    os.getenv(
        "SPARK_K8S_IMAGE",
        "registry.example.invalid/recsys/recsys-spark:required",
    ),
)
DRIFT_RETRAIN_IMAGE = os.getenv(
    "DRIFT_RETRAIN_IMAGE",
    "registry.example.invalid/recsys/recsys-drift-retrain:required",
)
DATAFLOW_NODE_SELECTOR = os.getenv(
    "DATAFLOW_NODE_SELECTOR",
    "recsys.ai/pool=cpu-services",
)
COMMON_ENV = {
    # KPO env vars override the image-level PYTHONPATH, so every source-backed
    # runtime imported by data-platform tasks must remain explicit here.
    "PYTHONPATH": "/opt/recsys/apps/data-platform/src:/opt/recsys/packages/recsys-feature-store-runtime/src:/opt/recsys/packages/recsys-rag-runtime/src:/opt/recsys",
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
    "OFFLINE_FEATURE_DRIFT_CURRENT_ROOT",
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
)
SPARK_SECRET_ENV = (
    "MINIO_ROOT_USER",
    "MINIO_ROOT_PASSWORD",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
)


def env_schedule(name: str, default: str | None):
    schedule = os.getenv(name, default or "")
    if schedule.lower() in {"", "none", "manual"}:
        return None
    return schedule


def pod_env_from():
    if k8s is None:
        return []
    return [
        k8s.V1EnvFromSource(
            config_map_ref=k8s.V1ConfigMapEnvSource(name="recsys-data-platform-config")
        ),
        k8s.V1EnvFromSource(
            secret_ref=k8s.V1SecretEnvSource(name="recsys-data-platform-secret")
        ),
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


def pod_task(
    task_id: str,
    image: str,
    command: str,
    *,
    istio_inject: bool = False,
):
    return KubernetesPodOperator(
        task_id=task_id,
        name=task_id.replace("_", "-"),
        namespace=NAMESPACE,
        image=image,
        cmds=["bash", "-c"],
        arguments=[f"set -euo pipefail; {command}"],
        env_vars=COMMON_ENV,
        env_from=pod_env_from(),
        annotations={"sidecar.istio.io/inject": str(istio_inject).lower()},
        node_selector=parse_node_selector(DATAFLOW_NODE_SELECTOR),
        get_logs=True,
        on_finish_action="delete_succeeded_pod",
        in_cluster=True,
        startup_timeout_seconds=600,
    )


def spark_native_submit(
    task_id: str,
    application: str,
    application_args: str = "",
) -> str:
    app_name = f"recsys-{task_id.replace('_', '-')}"
    env_conf = " ".join(
        f"--conf spark.kubernetes.driverEnv.{name}=${{{name}:-}} "
        f"--conf spark.executorEnv.{name}=${{{name}:-}}"
        for name in SPARK_DRIVER_EXECUTOR_ENV
    )
    secret_conf = " ".join(
        f"--conf spark.kubernetes.driver.secretKeyRef.{name}="
        f"recsys-data-platform-secret:{name} "
        f"--conf spark.kubernetes.executor.secretKeyRef.{name}="
        f"recsys-data-platform-secret:{name}"
        for name in SPARK_SECRET_ENV
    )
    return (
        'SPARK_APP_SUFFIX="$(date +%s)-${RANDOM}"; '
        'SPARK_SUBMIT_LOG="$(mktemp)"; '
        "/opt/spark/bin/spark-submit "
        "--master ${SPARK_K8S_MASTER:-k8s://https://kubernetes.default.svc} "
        "--deploy-mode cluster "
        f"--name {app_name}-${{SPARK_APP_SUFFIX}} "
        "--conf spark.kubernetes.namespace=${SPARK_K8S_NAMESPACE:-recsys-dataflow} "
        "--conf spark.kubernetes.container.image=${SPARK_K8S_IMAGE:-registry.example.invalid/recsys/recsys-spark:required} "
        "--conf spark.kubernetes.container.image.pullPolicy=${SPARK_K8S_IMAGE_PULL_POLICY:-IfNotPresent} "
        "--conf spark.kubernetes.authenticate.driver.serviceAccountName=${SPARK_K8S_SERVICE_ACCOUNT:-default} "
        "--conf spark.kubernetes.submission.waitAppCompletion=true "
        "--conf spark.kubernetes.submission.connectionTimeout=${SPARK_K8S_CONNECTION_TIMEOUT:-60000} "
        "--conf spark.kubernetes.submission.requestTimeout=${SPARK_K8S_REQUEST_TIMEOUT:-180000} "
        "--conf spark.kubernetes.report.interval=5s "
        "--conf spark.sql.iceberg.vectorization.enabled=${SPARK_ICEBERG_VECTORIZATION_ENABLED:-false} "
        "--conf spark.kubernetes.driver.annotation.sidecar.istio.io/inject=false "
        "--conf spark.kubernetes.executor.annotation.sidecar.istio.io/inject=false "
        "--conf spark.kubernetes.node.selector.recsys.ai/pool=${SPARK_K8S_NODE_POOL:-cpu-services} "
        "--conf spark.driver.memory=${SPARK_K8S_DRIVER_MEMORY:-1g} "
        "--conf spark.driver.memoryOverhead=${SPARK_K8S_DRIVER_MEMORY_OVERHEAD:-384m} "
        "--conf spark.driver.cores=${SPARK_K8S_DRIVER_CORES:-1} "
        "--conf spark.kubernetes.driver.request.cores=${SPARK_K8S_DRIVER_REQUEST_CORES:-500m} "
        "--conf spark.executor.instances=${SPARK_K8S_EXECUTOR_INSTANCES:-1} "
        "--conf spark.dynamicAllocation.enabled=${SPARK_DYNAMIC_ALLOCATION_ENABLED:-true} "
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
        f"{application} {application_args} "
        '2>&1 | tee "$SPARK_SUBMIT_LOG"; '
        'grep -q "phase: Succeeded" "$SPARK_SUBMIT_LOG"'
    )
