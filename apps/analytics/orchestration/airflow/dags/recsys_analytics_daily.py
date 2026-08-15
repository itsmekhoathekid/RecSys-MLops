from __future__ import annotations

import os
from datetime import timedelta

from metadata.governance_catalog import BRONZE_URNS, SILVER_URNS, dataset_urn
from orchestration.airflow.spark_utils import datahub_urns

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
SILVER_SOURCE_TABLES = (
    "clean_behavior_events",
    "clean_impressions",
    "clean_recommendation_requests",
    "product_scd",
    "users",
    "products",
)
BRONZE_SOURCE_TABLES = ("orders", "order_items")
ANALYTICS_STAGING_URNS = tuple(
    dataset_urn("iceberg", f"analytics.staging.{table}")
    for table in SILVER_SOURCE_TABLES + BRONZE_SOURCE_TABLES
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
    inlets=(),
    outlets=(),
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
        env_vars={"RUNTIME_LINEAGE_ENABLED": "false"},
        inlets=datahub_urns(inlets),
        outlets=datahub_urns(outlets),
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
            ["local:///opt/recsys/apps/analytics/src/sync_silver.py"],
            inlets=(
                *(SILVER_URNS[name] for name in SILVER_SOURCE_TABLES),
                *(BRONZE_URNS[name] for name in BRONZE_SOURCE_TABLES),
            ),
            outlets=ANALYTICS_STAGING_URNS,
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
        sync_silver >> dbt_build
