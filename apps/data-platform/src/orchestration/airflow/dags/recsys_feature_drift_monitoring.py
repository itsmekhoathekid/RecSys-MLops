from __future__ import annotations

from orchestration.airflow.spark_utils import (
    DAG,
    DRIFT_RETRAIN_IMAGE,
    datetime,
    env_schedule,
    pod_task,
)


RUN_OFFLINE_FEATURE_DRIFT_COMMAND = (
    "python -m validate.offline_feature_drift "
    ' --report-path "$OFFLINE_FEATURE_DRIFT_REPORT_PATH" '
    ' --current-feature-root "$OFFLINE_FEATURE_DRIFT_CURRENT_ROOT" '
    ' --baseline-path "$OFFLINE_FEATURE_DRIFT_BASELINE_PATH" '
    ' --sample-rows "$OFFLINE_FEATURE_DRIFT_SAMPLE_ROWS" '
    ' --current-days "$OFFLINE_FEATURE_DRIFT_CURRENT_DAYS" '
    ' --threshold "$RETRAIN_PSI_THRESHOLD" '
    ' --pushgateway-url "${PUSHGATEWAY_URL:-}"'
)

PUSH_DRIFT_METRICS_COMMAND = (
    "python -m monitoring.push_drift_report_metrics "
    ' --drift-report-path "$OFFLINE_FEATURE_DRIFT_REPORT_PATH" '
    ' --pushgateway-url "${PUSHGATEWAY_URL:-}"'
)

TRIGGER_KUBEFLOW_RETRAIN_COMMAND = (
    "python -m mlops.trigger_kubeflow_retrain "
    ' --drift-report-path "$OFFLINE_FEATURE_DRIFT_REPORT_PATH" '
    ' --kfp-endpoint "$KFP_ENDPOINT" '
    ' --experiment-name "$KFP_EXPERIMENT_NAME" '
    ' --pipeline-name "$KFP_PIPELINE_NAME" '
    ' --pipeline-version-id "${KFP_PIPELINE_VERSION_ID:-}" '
    ' --pushgateway-url "${PUSHGATEWAY_URL:-}" '
    "--fail-on-trigger-error"
)


if DAG is not None:
    with DAG(
        dag_id="recsys_feature_drift_monitoring",
        start_date=datetime(2026, 1, 1),
        schedule=env_schedule("FEATURE_DRIFT_DAG_SCHEDULE", "30 3 * * *"),
        catchup=False,
        max_active_runs=1,
        is_paused_upon_creation=False,
        tags=["recsys", "drift", "monitoring", "retrain"],
    ) as recsys_feature_drift_monitoring:
        run_drift = pod_task(
            "run_offline_feature_drift",
            DRIFT_RETRAIN_IMAGE,
            RUN_OFFLINE_FEATURE_DRIFT_COMMAND,
            istio_inject=True,
        )
        push_metrics = pod_task(
            "push_drift_metrics",
            DRIFT_RETRAIN_IMAGE,
            PUSH_DRIFT_METRICS_COMMAND,
            istio_inject=True,
        )
        trigger_retrain = pod_task(
            "trigger_kubeflow_retrain_if_drift",
            DRIFT_RETRAIN_IMAGE,
            TRIGGER_KUBEFLOW_RETRAIN_COMMAND,
            istio_inject=True,
        )

        run_drift >> push_metrics >> trigger_retrain
