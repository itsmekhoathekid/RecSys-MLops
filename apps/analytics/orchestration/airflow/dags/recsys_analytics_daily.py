from __future__ import annotations

import os
from datetime import timedelta

try:
    from airflow import DAG
    from airflow.providers.cncf.kubernetes.operators.pod import KubernetesPodOperator
    from kubernetes.client import models as k8s
    from pendulum import datetime
except ImportError:  # pragma: no cover
    DAG = KubernetesPodOperator = datetime = k8s = None


NAMESPACE = os.getenv("ANALYTICS_NAMESPACE", "analytics")
SPARK_IMAGE = os.getenv(
    "SPARK_IMAGE", "registry.example.invalid/recsys/recsys-spark:required"
)
DBT_IMAGE = os.getenv(
    "ANALYTICS_DBT_IMAGE",
    "registry.example.invalid/recsys/recsys-analytics-dbt:required",
)
DATA_INGESTION_IMAGE = os.getenv(
    "DATA_INGESTION_IMAGE",
    "registry.example.invalid/recsys/recsys-data-ingestion:required",
)
REPORT_URI = (
    "s3://recsys-lakehouse/governance-validation/ANALYTICS/{{ ts_nodash }}/staging.json"
)
ANALYTICS_DATASET_KEYS = tuple(
    f"analytics.staging.{table}"
    for table in (
        "clean_behavior_events",
        "clean_impressions",
        "clean_recommendation_requests",
        "product_scd",
        "users",
        "products",
        "orders",
        "order_items",
    )
)


def env_schedule(name: str, default: str | None):
    schedule = os.getenv(name, default or "")
    if schedule.lower() in {"", "none", "manual"}:
        return None
    return schedule


def analytics_env_from():
    if k8s is None:
        return []
    return [
        k8s.V1EnvFromSource(
            config_map_ref=k8s.V1ConfigMapEnvSource(name="recsys-analytics-config")
        ),
        k8s.V1EnvFromSource(
            secret_ref=k8s.V1SecretEnvSource(name="recsys-analytics-secret")
        ),
    ]


def analytics_task(
    task_id: str,
    image: str,
    command: list[str],
    arguments: list[str],
    *,
    trigger_rule: str = "all_success",
):
    return KubernetesPodOperator(
        task_id=task_id,
        name=task_id.replace("_", "-"),
        namespace=NAMESPACE,
        image=image,
        cmds=command,
        arguments=arguments,
        env_from=analytics_env_from(),
        annotations={"sidecar.istio.io/inject": "false"},
        node_selector={"recsys.ai/pool": "cpu-services"},
        image_pull_policy=os.getenv("ANALYTICS_IMAGE_PULL_POLICY", "IfNotPresent"),
        get_logs=True,
        # Keep failed pods for diagnosis; delete a successful pod only after the
        # operator has observed its terminal state.  The deprecated boolean
        # cleanup flag can delete the pod while it is still being polled, which
        # turns an otherwise successful task into a 404.
        on_finish_action="delete_succeeded_pod",
        in_cluster=True,
        startup_timeout_seconds=600,
        trigger_rule=trigger_rule,
    )


if DAG is not None:
    with DAG(
        dag_id="recsys_analytics_daily",
        start_date=datetime(2026, 1, 1, tz="UTC"),
        schedule=env_schedule("ANALYTICS_DAG_SCHEDULE", "30 2 * * *"),
        catchup=False,
        max_active_runs=1,
        default_args={
            "retries": 2,
            "retry_delay": timedelta(minutes=5),
        },
        tags=["analytics", "iceberg", "dbt"],
    ) as recsys_analytics_daily:
        sync_silver = analytics_task(
            "sync_silver_catalog",
            SPARK_IMAGE,
            ["/opt/spark/bin/spark-submit"],
            [
                "local:///opt/recsys/apps/analytics/src/sync_silver.py",
                "--report-uri",
                REPORT_URI,
                "--run-id",
                "{{ run_id }}",
            ],
        )
        dbt_build = analytics_task(
            "build_gold_marts",
            DBT_IMAGE,
            ["dbt"],
            [
                "build",
                "--project-dir",
                "/opt/recsys/apps/analytics",
                "--profiles-dir",
                "/opt/recsys/apps/analytics/profiles",
            ],
        )
        publish_datahub_validation = analytics_task(
            "publish_datahub_validation",
            DATA_INGESTION_IMAGE,
            ["python", "-m", "metadata.publish_datahub_validation"],
            [
                "--product",
                "ANALYTICS",
                "--report-uri",
                REPORT_URI,
                *[
                    value
                    for key in ANALYTICS_DATASET_KEYS
                    for value in ("--expected-dataset-key", key)
                ],
                "--gms-url",
                os.getenv(
                    "DATAHUB_GMS_URL",
                    "http://datahub-datahub-gms.datahub.svc.cluster.local:8080",
                ),
                "--strict",
            ],
            trigger_rule="all_done",
        )
        sync_silver >> dbt_build >> publish_datahub_validation
