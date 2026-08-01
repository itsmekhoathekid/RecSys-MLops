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
    "--pipeline-name $KFP_PIPELINE_NAME "
    "--pipeline-version-id $KFP_PIPELINE_VERSION_ID "
    "--pushgateway-url $PUSHGATEWAY_URL "
    "--pipeline-arg source_run_path=s3a://$LAKE_BUCKET/raw/$DATA_GENERATOR_RUN_ID"
)


if DAG is not None:
    with DAG(
        dag_id="recsys_feature_drift_monitoring",
        start_date=datetime(2026, 1, 1),
        schedule=env_schedule("FEATURE_DRIFT_DAG_SCHEDULE", "30 3 * * *"),
        catchup=False,
        max_active_runs=1,
        tags=["recsys", "drift", "monitoring", "retrain"],
    ) as recsys_feature_drift_monitoring:
        run_drift = pod_task(
            "run_offline_feature_drift",
            DRIFT_RETRAIN_IMAGE,
            RUN_OFFLINE_FEATURE_DRIFT_COMMAND,
        )
        push_metrics = pod_task(
            "push_drift_metrics",
            DRIFT_RETRAIN_IMAGE,
            PUSH_DRIFT_METRICS_COMMAND,
        )
        trigger_retrain = pod_task(
            "trigger_kubeflow_retrain_if_drift",
            DRIFT_RETRAIN_IMAGE,
            TRIGGER_KUBEFLOW_RETRAIN_COMMAND,
        )

        run_drift >> push_metrics >> trigger_retrain
