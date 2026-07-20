from __future__ import annotations

import os

try:
    from airflow import DAG
    from airflow.operators.trigger_dagrun import TriggerDagRunOperator
    from airflow.providers.cncf.kubernetes.operators.pod import KubernetesPodOperator
    from kubernetes.client import models as k8s
    from pendulum import datetime
except ImportError:  # pragma: no cover
    DAG = TriggerDagRunOperator = KubernetesPodOperator = datetime = k8s = None


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
        "--conf spark.executor.memory=${SPARK_K8S_EXECUTOR_MEMORY:-1g} "
        "--conf spark.executor.memoryOverhead=${SPARK_K8S_EXECUTOR_MEMORY_OVERHEAD:-384m} "
        "--conf spark.executor.cores=${SPARK_K8S_EXECUTOR_CORES:-1} "
        "--conf spark.kubernetes.executor.request.cores=${SPARK_K8S_EXECUTOR_REQUEST_CORES:-500m} "
        f"{env_conf} {secret_conf} {application} {application_args}".strip()
    )


SPARK_BATCH_COMMAND = spark_native_submit(
    "operational_dp3_offline_feature_table",
    "local:///opt/recsys/apps/data-platform/src/features/spark/spark_batch_entrypoint.py",
    "--config $SPARK_BATCH_CONFIG",
)
DP2_INGEST_COMMAND = spark_native_submit(
    "operational_dp2_ingest",
    "local:///opt/recsys/apps/data-platform/src/features/spark/dp2_silver_gold_entrypoint.py",
    "--action ingest",
)
DP2_OPTIMIZE_COMMAND = spark_native_submit(
    "operational_dp2_optimize",
    "local:///opt/recsys/apps/data-platform/src/lakehouse/optimize.py",
    "--scope silver --pipeline DP2 "
    "--strategy ${LAKEHOUSE_OPTIMIZATION_STRATEGY:-binpack} "
    "--target-file-size-mb ${LAKEHOUSE_TARGET_FILE_SIZE_MB:-128} "
    "--min-input-files ${LAKEHOUSE_COMPACTION_MIN_INPUT_FILES:-2}",
)
DP2_VALIDATE_COMMAND = spark_native_submit(
    "operational_dp2_validate",
    "local:///opt/recsys/apps/data-platform/src/features/spark/dp2_silver_gold_entrypoint.py",
    "--action validate",
)
VERIFY_POSTGRES_OFFLINE_STORE_COMMAND = "python -m validate.governance_contracts dp3-postgres"


FEAST_ENV_EXPORTS = """
export FEAST_POSTGRES_HOST=${FEAST_POSTGRES_HOST:-feature-postgres}
export FEAST_POSTGRES_PORT=${FEAST_POSTGRES_PORT:-5432}
export FEAST_POSTGRES_DB=${FEAST_POSTGRES_DB:-feature_store}
export FEAST_POSTGRES_SCHEMA=${FEAST_POSTGRES_SCHEMA:-feature_store}
export FEAST_POSTGRES_USER=${FEAST_POSTGRES_USER:-feast}
export FEAST_POSTGRES_PASSWORD=${FEAST_POSTGRES_PASSWORD:-feast}
export FEAST_POSTGRES_SSLMODE=${FEAST_POSTGRES_SSLMODE:-disable}
""".strip()

APPLY_FEAST_FEATURE_REPO_COMMAND = f"""
cd /opt/recsys/apps/data-platform/feature-store/feature_repo
{FEAST_ENV_EXPORTS}
python -c 'from feature_store.feast_registry import apply_feature_repo; apply_feature_repo(".")'
""".strip()

FEAST_MATERIALIZE_INCREMENTAL_COMMAND = f"""
cd /opt/recsys/apps/data-platform/feature-store/feature_repo
{FEAST_ENV_EXPORTS}
feast materialize-incremental $(date -u +%Y-%m-%dT%H:%M:%S)
""".strip()

VERIFY_REDIS_ONLINE_STORE_COMMAND = "python -m validate.governance_contracts streaming-redis"

RUN_OFFLINE_FEATURE_DRIFT_COMMAND = (
    "python -m validate.offline_feature_drift "
    "--report-path $OFFLINE_FEATURE_DRIFT_REPORT_PATH "
    "--current-feature-root $OFFLINE_FEATURE_DRIFT_CURRENT_ROOT "
    "--baseline-path $OFFLINE_FEATURE_DRIFT_BASELINE_PATH "
    "--sample-rows $OFFLINE_FEATURE_DRIFT_SAMPLE_ROWS "
    "--current-days $OFFLINE_FEATURE_DRIFT_CURRENT_DAYS "
    "--threshold $RETRAIN_PSI_THRESHOLD "
    "--pushgateway-url $PUSHGATEWAY_URL"
)

PUSH_DRIFT_METRICS_COMMAND = r"""
python -c '
import json
import os
import time
from mlops.trigger_kubeflow_retrain import read_json
from monitoring.pushgateway import MetricSample, push_metrics

report = read_json(os.getenv("OFFLINE_FEATURE_DRIFT_REPORT_PATH"))
run_id = str(report.get("run_id") or "unknown")
samples = [
    MetricSample(
        "recsys_ml_feature_drift_report_available",
        1.0,
        {"run_id": run_id, "passed": str(report.get("passed", False)).lower()},
    ),
    MetricSample(
        "recsys_ml_feature_drift_report_timestamp_seconds",
        float(int(time.time())),
        {"run_id": run_id},
    ),
]
push_metrics(
    samples,
    job="recsys_offline_feature_drift_report",
    gateway_url=os.getenv("PUSHGATEWAY_URL"),
    grouping_key={"run_id": run_id},
)
print(json.dumps({"pushed_drift_report_metrics": True, "run_id": run_id}, sort_keys=True))
'
""".strip()

TRIGGER_KUBEFLOW_RETRAIN_COMMAND = (
    "python -m mlops.trigger_kubeflow_retrain "
    "--drift-report-path $OFFLINE_FEATURE_DRIFT_REPORT_PATH "
    "--kfp-endpoint $KFP_ENDPOINT "
    "--experiment-name $KFP_EXPERIMENT_NAME "
    "--pipeline-package-path $KFP_PIPELINE_PACKAGE_PATH "
    "--pushgateway-url $PUSHGATEWAY_URL "
    "--pipeline-arg source_run_path=s3a://$LAKE_BUCKET/raw/$DATA_GENERATOR_RUN_ID"
)


def trigger_dag(task_id: str, dag_id: str):
    return TriggerDagRunOperator(
        task_id=task_id,
        trigger_dag_id=dag_id,
        wait_for_completion=True,
        poke_interval=30,
        reset_dag_run=True,
        allowed_states=["success"],
        failed_states=["failed"],
    )


if DAG is not None:
    with DAG(
        dag_id="k8s_data_platform_dag",
        start_date=datetime(2026, 1, 1),
        schedule=env_schedule("DATA_PLATFORM_DAG_SCHEDULE", "@daily"),
        catchup=False,
        max_active_runs=1,
        tags=["recsys", "k8s", "full-cycle", "lakehouse"],
    ) as k8s_data_platform_dag:
        run_dp1 = trigger_dag("run_dp1", "recsys_dp1_raw_to_bronze")
        run_dp2 = trigger_dag("run_dp2", "recsys_dp2_bronze_to_silver_gold")
        run_dp3 = trigger_dag("run_dp3", "recsys_dp3_offline_feature_table")
        materialize_online = trigger_dag("materialize_online", "recsys_feast_materialize")
        build_analytics = trigger_dag("build_analytics", "recsys_analytics_daily")
        check_drift = trigger_dag("check_drift", "recsys_feature_drift_monitoring")

        run_dp1 >> run_dp2 >> run_dp3 >> materialize_online >> build_analytics >> check_drift

    with DAG(
        dag_id="recsys_batch_feature_pipeline",
        start_date=datetime(2026, 1, 1),
        schedule=env_schedule("BATCH_FEATURE_DAG_SCHEDULE", "0 1 * * *"),
        catchup=False,
        max_active_runs=1,
        tags=["recsys", "features", "iceberg", "offline-store"],
    ) as recsys_batch_feature_pipeline:
        ingest_silver = pod_task("ingest_silver", SPARK_IMAGE, DP2_INGEST_COMMAND)
        optimize_silver = pod_task("optimize_silver", SPARK_IMAGE, DP2_OPTIMIZE_COMMAND)
        validate_silver = pod_task("validate_silver", SPARK_IMAGE, DP2_VALIDATE_COMMAND)
        build_offline_features = pod_task(
            "build_offline_features", SPARK_IMAGE, SPARK_BATCH_COMMAND
        )
        validate_offline_store = pod_task(
            "validate_offline_store", DATAFLOW_IMAGE, VERIFY_POSTGRES_OFFLINE_STORE_COMMAND
        )

        (
            ingest_silver
            >> optimize_silver
            >> validate_silver
            >> build_offline_features
            >> validate_offline_store
        )

    with DAG(
        dag_id="recsys_feast_materialize",
        start_date=datetime(2026, 1, 1),
        schedule=env_schedule("FEAST_MATERIALIZE_DAG_SCHEDULE", "20 */2 * * *"),
        catchup=False,
        max_active_runs=1,
        tags=["recsys", "feast", "materialize", "online-store"],
    ) as recsys_feast_materialize:
        apply_feature_repo = pod_task(
            "apply_feast_feature_repo", DATAFLOW_IMAGE, APPLY_FEAST_FEATURE_REPO_COMMAND
        )
        materialize_incremental = pod_task(
            "feast_materialize_incremental",
            DATAFLOW_IMAGE,
            FEAST_MATERIALIZE_INCREMENTAL_COMMAND,
        )
        validate_online_store = pod_task(
            "verify_redis_online_store_updated",
            DATAFLOW_IMAGE,
            VERIFY_REDIS_ONLINE_STORE_COMMAND,
        )

        apply_feature_repo >> materialize_incremental >> validate_online_store

    with DAG(
        dag_id="recsys_feature_drift_monitoring",
        start_date=datetime(2026, 1, 1),
        schedule=env_schedule("FEATURE_DRIFT_DAG_SCHEDULE", "30 3 * * *"),
        catchup=False,
        max_active_runs=1,
        tags=["recsys", "drift", "monitoring", "retrain"],
    ) as recsys_feature_drift_monitoring:
        run_drift = pod_task(
            "run_offline_feature_drift", DATAFLOW_IMAGE, RUN_OFFLINE_FEATURE_DRIFT_COMMAND
        )
        push_metrics = pod_task(
            "push_drift_metrics", DATAFLOW_IMAGE, PUSH_DRIFT_METRICS_COMMAND
        )
        trigger_retrain = pod_task(
            "trigger_kubeflow_retrain_if_drift",
            DATAFLOW_IMAGE,
            TRIGGER_KUBEFLOW_RETRAIN_COMMAND,
        )

        run_drift >> push_metrics >> trigger_retrain
