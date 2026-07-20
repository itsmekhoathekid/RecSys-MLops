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
DATAFLOW_IMAGE = os.getenv("DATAFLOW_IMAGE", "recsys-dataflow-cli:local")
DATAFLOW_NODE_SELECTOR = os.getenv("DATAFLOW_NODE_SELECTOR", "recsys.ai/pool=cpu-services")
COMMON_ENV = {
    "PYTHONPATH": "/opt/recsys/apps/data-platform/src:/opt/recsys",
    "VALIDATION_RUN_ID": "{{ run_id }}",
    "RUNTIME_LINEAGE_ENABLED": "true",
    "RUNTIME_LINEAGE_STRICT": "true",
}
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


if DAG is not None:
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
