from __future__ import annotations

import os
from typing import Mapping

from kfp import kubernetes


DEFAULT_PVC_NAME = os.getenv("RECSYS_KFP_PVC_NAME", "recsys-mlops-pvc")
DEFAULT_PVC_MOUNT_PATH = os.getenv("RECSYS_KFP_PVC_MOUNT_PATH", "/workspace")
DEFAULT_RUNTIME_SECRET_NAME = os.getenv("RECSYS_KFP_RUNTIME_SECRET_NAME", "recsys-mlops-runtime")
DEFAULT_NODE_SELECTOR = os.getenv("RECSYS_KFP_NODE_SELECTOR", "recsys.ai/pool=ml-system")
DEFAULT_TOLERATION_KEY = os.getenv("RECSYS_KFP_TOLERATION_KEY", "recsys.ai/workload")
DEFAULT_TOLERATION_VALUE = os.getenv("RECSYS_KFP_TOLERATION_VALUE", "ml-system")
DEFAULT_TOLERATION_EFFECT = os.getenv("RECSYS_KFP_TOLERATION_EFFECT", "NoSchedule")

SECRET_KEY_TO_ENV: dict[str, str] = {
    "MINIO_ENDPOINT": "MINIO_ENDPOINT",
    "MINIO_ROOT_USER": "MINIO_ROOT_USER",
    "MINIO_ROOT_PASSWORD": "MINIO_ROOT_PASSWORD",
    "MLFLOW_TRACKING_URI": "MLFLOW_TRACKING_URI",
    "MLFLOW_EXPERIMENT_NAME": "MLFLOW_EXPERIMENT_NAME",
    "MLFLOW_S3_ENDPOINT_URL": "MLFLOW_S3_ENDPOINT_URL",
    "MODEL_STORE_ENDPOINT": "MODEL_STORE_ENDPOINT",
    "MODEL_REGISTRY_POSTGRES_URI": "MODEL_REGISTRY_POSTGRES_URI",
    "MODEL_STORE_BUCKET": "MODEL_STORE_BUCKET",
    "MODEL_STORE_PREFIX": "MODEL_STORE_PREFIX",
    "PROMOTION_MANIFEST_KEY": "PROMOTION_MANIFEST_KEY",
    "AWS_ACCESS_KEY_ID": "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY": "AWS_SECRET_ACCESS_KEY",
    "AWS_DEFAULT_REGION": "AWS_DEFAULT_REGION",
    "ICEBERG_ENABLED": "ICEBERG_ENABLED",
    "ICEBERG_CATALOG_NAME": "ICEBERG_CATALOG_NAME",
    "ICEBERG_WAREHOUSE": "ICEBERG_WAREHOUSE",
    "HUDI_ENABLED": "HUDI_ENABLED",
    "HUDI_CATALOG_NAME": "HUDI_CATALOG_NAME",
    "HUDI_WAREHOUSE": "HUDI_WAREHOUSE",
}


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


def wire_runtime(
    task,
    pvc_name: str = DEFAULT_PVC_NAME,
    mount_path: str = DEFAULT_PVC_MOUNT_PATH,
    secret_name: str = DEFAULT_RUNTIME_SECRET_NAME,
    secret_key_to_env: Mapping[str, str] = SECRET_KEY_TO_ENV,
    node_selector: str = DEFAULT_NODE_SELECTOR,
):
    if hasattr(task, "platform_config"):
        kubernetes.add_pod_annotation(task, "sidecar.istio.io/inject", "false")
        kubernetes.set_image_pull_policy(task, "Always")
        for key, value in parse_node_selector(node_selector).items():
            kubernetes.add_node_selector(task, key, value)
        if DEFAULT_TOLERATION_KEY and DEFAULT_TOLERATION_VALUE:
            kubernetes.add_toleration(
                task,
                key=DEFAULT_TOLERATION_KEY,
                operator="Equal",
                value=DEFAULT_TOLERATION_VALUE,
                effect=DEFAULT_TOLERATION_EFFECT,
            )
    kubernetes.mount_pvc(task, pvc_name=pvc_name, mount_path=mount_path)
    kubernetes.use_secret_as_env(
        task,
        secret_name=secret_name,
        secret_key_to_env=dict(secret_key_to_env),
    )
    task.set_caching_options(False)
    return task
