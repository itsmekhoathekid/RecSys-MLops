from __future__ import annotations

import os
from typing import Mapping

from kfp import kubernetes


DEFAULT_PVC_NAME = os.getenv("RECSYS_KFP_PVC_NAME", "recsys-mlops-pvc")
DEFAULT_PVC_MOUNT_PATH = os.getenv("RECSYS_KFP_PVC_MOUNT_PATH", "/workspace")
DEFAULT_RUNTIME_SECRET_NAME = os.getenv("RECSYS_KFP_RUNTIME_SECRET_NAME", "recsys-mlops-runtime")

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


def wire_runtime(
    task,
    pvc_name: str = DEFAULT_PVC_NAME,
    mount_path: str = DEFAULT_PVC_MOUNT_PATH,
    secret_name: str = DEFAULT_RUNTIME_SECRET_NAME,
    secret_key_to_env: Mapping[str, str] = SECRET_KEY_TO_ENV,
):
    kubernetes.mount_pvc(task, pvc_name=pvc_name, mount_path=mount_path)
    kubernetes.use_secret_as_env(
        task,
        secret_name=secret_name,
        secret_key_to_env=dict(secret_key_to_env),
    )
    task.set_caching_options(False)
    return task
