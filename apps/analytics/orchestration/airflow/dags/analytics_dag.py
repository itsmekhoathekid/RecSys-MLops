from __future__ import annotations

import os

try:
    from airflow import DAG
    from airflow.providers.cncf.kubernetes.operators.pod import KubernetesPodOperator
    from kubernetes.client import models as k8s
    from pendulum import datetime
except ImportError:  # pragma: no cover
    DAG = KubernetesPodOperator = datetime = k8s = None


NAMESPACE = os.getenv("ANALYTICS_NAMESPACE", "analytics")
SPARK_IMAGE = os.getenv("ANALYTICS_SPARK_IMAGE", "recsys-analytics-spark:local")
DBT_IMAGE = os.getenv("ANALYTICS_DBT_IMAGE", "recsys-analytics-dbt:local")


def analytics_env_from():
    if k8s is None:
        return []
    return [
        k8s.V1EnvFromSource(config_map_ref=k8s.V1ConfigMapEnvSource(name="recsys-analytics-config")),
        k8s.V1EnvFromSource(secret_ref=k8s.V1SecretEnvSource(name="recsys-analytics-secret")),
    ]


def analytics_task(task_id: str, image: str, command: list[str], arguments: list[str]):
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
        is_delete_operator_pod=True,
        in_cluster=True,
        startup_timeout_seconds=600,
    )


if DAG is not None:
    with DAG(
        dag_id="recsys_analytics_daily",
        start_date=datetime(2025, 1, 1, tz="UTC"),
        schedule=os.getenv("ANALYTICS_DAG_SCHEDULE", "30 2 * * *"),
        catchup=False,
        max_active_runs=1,
        tags=["analytics", "iceberg", "dbt"],
    ) as analytics_dag:
        sync_silver = analytics_task(
            "sync_silver_catalog",
            SPARK_IMAGE,
            ["/opt/spark/bin/spark-submit"],
            ["local:///opt/recsys/apps/analytics/src/sync_silver.py"],
        )
        dbt_build = analytics_task(
            "build_gold_marts",
            DBT_IMAGE,
            ["dbt"],
            ["build", "--profiles-dir", "/opt/recsys/apps/analytics/profiles"],
        )
        sync_silver >> dbt_build

