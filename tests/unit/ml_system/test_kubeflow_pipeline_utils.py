from __future__ import annotations

import argparse

from kubeflow.components import runtime
from kubeflow.pipelines.compile_training_pipeline import compile_pipeline
from submit_ray_job import build_rayjob, container_spec, pod_template


def test_secret_env_mapping_is_stable():
    assert runtime.SECRET_KEY_TO_ENV == {
        "MINIO_ENDPOINT": "MINIO_ENDPOINT",
        "MINIO_ROOT_USER": "MINIO_ROOT_USER",
        "MINIO_ROOT_PASSWORD": "MINIO_ROOT_PASSWORD",
        "MLFLOW_TRACKING_URI": "MLFLOW_TRACKING_URI",
        "MLFLOW_EXPERIMENT_NAME": "MLFLOW_EXPERIMENT_NAME",
        "MLFLOW_S3_ENDPOINT_URL": "MLFLOW_S3_ENDPOINT_URL",
        "MODEL_REGISTRY_POSTGRES_URI": "MODEL_REGISTRY_POSTGRES_URI",
        "AWS_ACCESS_KEY_ID": "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY": "AWS_SECRET_ACCESS_KEY",
        "AWS_DEFAULT_REGION": "AWS_DEFAULT_REGION",
    }


def test_wire_runtime_mounts_pvc_secret_and_disables_caching(monkeypatch):
    calls: list[tuple[str, object, dict[str, object]]] = []

    class Task:
        caching_enabled = True

        def set_caching_options(self, enabled: bool) -> None:
            self.caching_enabled = enabled

    def mount_pvc(task, **kwargs):
        calls.append(("mount_pvc", task, kwargs))

    def use_secret_as_env(task, **kwargs):
        calls.append(("use_secret_as_env", task, kwargs))

    monkeypatch.setattr(runtime.kubernetes, "mount_pvc", mount_pvc)
    monkeypatch.setattr(runtime.kubernetes, "use_secret_as_env", use_secret_as_env)

    task = Task()
    result = runtime.wire_runtime(
        task,
        pvc_name="test-pvc",
        mount_path="/workspace",
        secret_name="runtime-secret",
    )

    assert result is task
    assert task.caching_enabled is False
    assert calls == [
        (
            "mount_pvc",
            task,
            {"pvc_name": "test-pvc", "mount_path": "/workspace"},
        ),
        (
            "use_secret_as_env",
            task,
            {"secret_name": "runtime-secret", "secret_key_to_env": runtime.SECRET_KEY_TO_ENV},
        ),
    ]


def test_rayjob_container_spec_cpu_contract():
    spec = container_spec(
        name="ray-head",
        image="recsys-training:test",
        cpu_request="500m",
        cpu_limit="1",
        memory_request="1Gi",
        memory_limit="2Gi",
        pvc_name="recsys-pvc",
        runtime_secret_name="runtime-secret",
        use_gpu=False,
        gpu_limit=0,
    )

    assert spec["resources"] == {
        "requests": {"cpu": "500m", "memory": "1Gi"},
        "limits": {"cpu": "1", "memory": "2Gi"},
    }
    assert spec["envFrom"] == [{"secretRef": {"name": "runtime-secret"}}]
    assert spec["volumeMounts"] == [{"name": "recsys-workspace", "mountPath": "/workspace"}]


def test_rayjob_gpu_worker_template_contract():
    worker = container_spec(
        name="ray-worker",
        image="recsys-training:test",
        cpu_request="1",
        cpu_limit="2",
        memory_request="2Gi",
        memory_limit="4Gi",
        pvc_name="recsys-pvc",
        runtime_secret_name="runtime-secret",
        use_gpu=True,
        gpu_limit=1,
    )
    template = pod_template(worker, pvc_name="recsys-pvc", use_gpu=True)

    assert worker["resources"]["requests"]["nvidia.com/gpu"] == 1
    assert worker["resources"]["limits"]["nvidia.com/gpu"] == 1
    assert template["spec"]["nodeSelector"] == {"nvidia.com/gpu.present": "true"}
    assert template["spec"]["tolerations"] == [
        {"key": "nvidia.com/gpu", "operator": "Exists", "effect": "NoSchedule"}
    ]


def test_build_rayjob_uses_refactored_training_module():
    args = argparse.Namespace(
        base_config_path="/opt/recsys/configs/local/bst.yaml",
        split_dir="/workspace/recsys/notebooks/data/bst_split",
        ray_output_dir="/workspace/recsys/data_platform/output/ml/ray",
        training_percent=0.01,
        num_epochs=1,
        max_trials=2,
        parallel_trials=1,
        cpus_per_trial=1.0,
        gpus_per_trial=0.0,
        use_gpu=False,
        best_result_path="/workspace/recsys/data_platform/output/ml/ray/best_result.json",
        image="recsys-training:test",
        head_cpu_request="500m",
        head_cpu_limit="1",
        head_memory_request="1Gi",
        head_memory_limit="2Gi",
        pvc_name="recsys-pvc",
        runtime_secret_name="runtime-secret",
        worker_cpu_request="1",
        worker_cpu_limit="2",
        worker_memory_request="2Gi",
        worker_memory_limit="4Gi",
        gpu_limit=1,
        job_name="recsys-bst-ray-tune",
        namespace="kubeflow",
        ttl_seconds_after_finished=300,
        worker_replicas=1,
    )

    rayjob = build_rayjob(args)

    assert "python /opt/recsys/apps/ml-system/src/ray_tune_train_bst.py" in rayjob["spec"]["entrypoint"]
    assert "pipelines.model_pipeline" not in rayjob["spec"]["entrypoint"]


def test_compile_pipeline_writes_refactored_component_commands():
    package_path = compile_pipeline()
    compiled = package_path.read_text(encoding="utf-8")

    assert package_path.name == "bst_training_pipeline.yaml"
    assert "/opt/recsys/apps/ml-system/src/run_feature_engineering.py" in compiled
    assert "/opt/recsys/apps/ml-system/src/prepare_bst_training_data.py" in compiled
    assert "/opt/recsys/apps/ml-system/src/submit_ray_job.py" in compiled
    assert "/opt/recsys/apps/ml-system/src/evaluate_ray_best_bst.py" in compiled
    assert "pipelines.model_pipeline" not in compiled
    assert "recsys_model_pipeline" not in compiled
