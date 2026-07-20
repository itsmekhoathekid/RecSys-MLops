from __future__ import annotations

try:
    from airflow import DAG
    from airflow.operators.trigger_dagrun import TriggerDagRunOperator
    from pendulum import datetime
except ImportError:  # pragma: no cover
    DAG = TriggerDagRunOperator = datetime = None

from orchestration.airflow.dags.rubric_data_pipeline_dags import (
    DATAFLOW_IMAGE,
    DP2_INGEST_COMMAND,
    DP2_OPTIMIZE_COMMAND,
    DP2_VALIDATE_COMMAND,
    SPARK_BATCH_COMMAND,
    SPARK_IMAGE,
    VERIFY_POSTGRES_OFFLINE_STORE_COMMAND,
    env_schedule,
    pod_task,
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
