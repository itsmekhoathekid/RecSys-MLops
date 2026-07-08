from __future__ import annotations

import argparse
import json

import torch

from kubeflow.components import runtime
from kubeflow.pipelines.compile_training_pipeline import compile_pipeline
from kubeflow.upload_pipeline_package import upload_or_version_pipeline
from kubeflow.validate_pipeline_package import validate_pipeline_package
from cli.submit_ray_job import build_rayjob, container_spec, parse_toleration, pod_template, reusable_best_result
from training.ray_distributed_train_bst import ModelLifecycleService
from training.ray_tune_train_bst import best_payload_from_trial_outputs


def test_secret_env_mapping_is_stable():
    assert runtime.SECRET_KEY_TO_ENV == {
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
    assert {"name": "RAY_memory_usage_threshold", "value": "0.99"} in spec["env"]
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


def test_parse_toleration_supports_equal_exists_and_lists():
    assert parse_toleration(
        "recsys.ai/workload:ml-system:NoSchedule,node.kubernetes.io/memory-pressure::NoSchedule"
    ) == [
        {
            "key": "recsys.ai/workload",
            "operator": "Equal",
            "value": "ml-system",
            "effect": "NoSchedule",
        },
        {
            "key": "node.kubernetes.io/memory-pressure",
            "operator": "Exists",
            "effect": "NoSchedule",
        },
    ]


def test_build_rayjob_uses_refactored_training_module():
    args = argparse.Namespace(
        base_config_path="/opt/recsys/configs/local/bst.yaml",
        split_dir="/workspace/recsys/data_platform/output/ml/bst_split",
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
        head_ray_num_cpus="0",
        head_ray_memory_bytes="1073741824",
        head_object_store_memory_bytes="268435456",
        pvc_name="recsys-pvc",
        runtime_secret_name="runtime-secret",
        worker_cpu_request="1",
        worker_cpu_limit="2",
        worker_memory_request="2Gi",
        worker_memory_limit="4Gi",
        worker_ray_memory_bytes="2147483648",
        worker_object_store_memory_bytes="536870912",
        gpu_limit=1,
        dataset_metadata_path="/workspace/recsys/data_platform/output/ml/bst_split/dataset_version_meta.json",
        job_name="recsys-bst-ray-tune",
        namespace="kubeflow",
        ttl_seconds_after_finished=300,
        worker_replicas=1,
    )

    rayjob = build_rayjob(args)

    assert "python /opt/recsys/apps/ml-system/src/training/ray_tune_train_bst.py" in rayjob["spec"]["entrypoint"]
    assert "pipelines.model_pipeline" not in rayjob["spec"]["entrypoint"]
    assert rayjob["spec"]["rayClusterSpec"]["headGroupSpec"]["rayStartParams"]["memory"] == "1073741824"
    submitter_spec = rayjob["spec"]["submitterPodTemplate"]["spec"]
    assert submitter_spec["restartPolicy"] == "Never"
    assert submitter_spec["containers"][0]["image"] == "recsys-training:test"
    assert submitter_spec["containers"][0]["resources"] == {
        "requests": {"cpu": "50m", "memory": "128Mi"},
        "limits": {"cpu": "500m", "memory": "512Mi"},
    }
    worker_group = rayjob["spec"]["rayClusterSpec"]["workerGroupSpecs"][0]
    assert worker_group["rayStartParams"]["object-store-memory"] == "536870912"


def test_build_rayjob_supports_distributed_training_mode():
    args = argparse.Namespace(
        job_mode="distributed-train",
        base_config_path="/opt/recsys/configs/local/bst.yaml",
        split_dir="/workspace/recsys/data_platform/output/ml/bst_split",
        ray_output_dir="/workspace/recsys/data_platform/output/ml/ray",
        training_percent=0.02,
        num_epochs=1,
        max_trials=1,
        parallel_trials=1,
        cpus_per_trial=1.0,
        gpus_per_trial=0.0,
        use_gpu=False,
        best_result_path="/workspace/recsys/data_platform/output/ml/ray/best_result.json",
        tune_result_path="/workspace/recsys/data_platform/output/ml/ray/tune_result.json",
        image="recsys-training:test",
        head_cpu_request="500m",
        head_cpu_limit="1",
        head_memory_request="1Gi",
        head_memory_limit="2Gi",
        head_ray_num_cpus="0",
        head_ray_memory_bytes="1073741824",
        head_object_store_memory_bytes="268435456",
        pvc_name="recsys-pvc",
        runtime_secret_name="runtime-secret",
        worker_cpu_request="1",
        worker_cpu_limit="2",
        worker_memory_request="2Gi",
        worker_memory_limit="4Gi",
        worker_ray_memory_bytes="2147483648",
        worker_object_store_memory_bytes="536870912",
        gpu_limit=1,
        dataset_metadata_path="/workspace/recsys/data_platform/output/ml/bst_split/dataset_version_meta.json",
        job_name="recsys-bst-ray-ddp-train",
        namespace="kubeflow",
        ttl_seconds_after_finished=300,
        worker_replicas=2,
        num_workers=2,
    )

    rayjob = build_rayjob(args)

    assert "python /opt/recsys/apps/ml-system/src/training/ray_distributed_train_bst.py" in rayjob["spec"]["entrypoint"]
    assert "--tune-result-path /workspace/recsys/data_platform/output/ml/ray/tune_result.json" in rayjob["spec"]["entrypoint"]
    assert "--num-workers 2" in rayjob["spec"]["entrypoint"]
    assert rayjob["metadata"]["labels"]["recsys.ai/ray-job-mode"] == "distributed-train"
    assert rayjob["spec"]["rayClusterSpec"]["workerGroupSpecs"][0]["replicas"] == 2


def test_model_lifecycle_service_reports_ddp_proof_metrics():
    service = ModelLifecycleService({}, torch.device("cpu"), rank=1, world_size=2)

    metrics = service.report_metrics({"val/ndcg@10": 0.42, "epoch": 0, "note": "ignored"})

    assert metrics["val_ndcg_at_10"] == 0.42
    assert metrics["epoch"] == 0
    assert metrics["world_size"] == 2
    assert metrics["rank"] == 1
    assert metrics["ddp_gradient_sync"] is True
    assert metrics["distributed_sampler"] is True
    assert "note" not in metrics


def test_reusable_best_result_requires_matching_dataset_versions(tmp_path):
    checkpoint = tmp_path / "checkpoints" / "BST"
    checkpoint.parent.mkdir()
    checkpoint.write_text("model", encoding="utf-8")
    metadata = {
        "splits": {
            "train": {
                "table": "recsys.ml.bst_training_samples",
                "snapshot_id": 11,
                "tag": "bst_training_run_1",
                "row_count": 100,
            }
        }
    }
    best_result = {
        "mlflow_run_id": "run-1",
        "checkpoint_path": str(checkpoint),
        "dataset_versions": {
            "train": {
                "table": "recsys.ml.bst_training_samples",
                "snapshot_id": 11,
                "tag": "bst_training_run_1",
                "row_count": 100,
            }
        },
    }
    metadata_path = tmp_path / "dataset_version_meta.json"
    best_result_path = tmp_path / "best_result.json"
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    best_result_path.write_text(json.dumps(best_result), encoding="utf-8")

    assert reusable_best_result(str(best_result_path), str(metadata_path)) == best_result

    metadata["splits"]["train"]["snapshot_id"] = 12
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    assert reusable_best_result(str(best_result_path), str(metadata_path)) is None


def test_ray_tune_best_payload_falls_back_to_trial_outputs(tmp_path):
    low_trial = tmp_path / "trials" / "trial-low"
    high_trial = tmp_path / "trials" / "trial-high"
    low_trial.mkdir(parents=True)
    high_trial.mkdir(parents=True)
    (low_trial / "bst_trial.yaml").write_text("model_args: {}\n", encoding="utf-8")
    (high_trial / "bst_trial.yaml").write_text("model_args: {}\n", encoding="utf-8")
    (low_trial / "training_result.json").write_text(
        json.dumps(
            {
                "checkpoint_path": str(low_trial / "checkpoint"),
                "artifact_uri": str(low_trial / "checkpoint"),
                "metrics": {"val/ndcg@10": 0.1, "val/loss": 0.7},
                "dataset_versions": {"train": {"snapshot_id": 1}},
            }
        ),
        encoding="utf-8",
    )
    (high_trial / "training_result.json").write_text(
        json.dumps(
            {
                "checkpoint_path": str(high_trial / "checkpoint"),
                "artifact_uri": str(high_trial / "checkpoint"),
                "metrics": {"val/ndcg@10": 0.4, "val/loss": 0.5},
                "dataset_versions": {"train": {"snapshot_id": 2}},
            }
        ),
        encoding="utf-8",
    )

    payload = best_payload_from_trial_outputs(tmp_path)

    assert payload["best_trial_name"] == "trial-high"
    assert payload["best_config_path"] == str(high_trial / "bst_trial.yaml")
    assert payload["checkpoint_path"] == str(high_trial / "checkpoint")
    assert payload["source"] == "kubeflow-ray-tune-output-fallback"
    assert payload["ray_metrics"]["val_ndcg_at_10"] == 0.4


def test_compile_pipeline_writes_refactored_component_commands(tmp_path, monkeypatch):
    training_image = "registry.example/recsys/recsys-mlops-training:test"
    spark_image = "registry.example/recsys/recsys-mlops-spark:test"
    monkeypatch.setenv("RECSYS_PIPELINE_IMAGE", training_image)
    monkeypatch.setenv("RECSYS_RAY_IMAGE", training_image)
    monkeypatch.setenv("RECSYS_SPARK_IMAGE", spark_image)

    package_path = compile_pipeline(tmp_path / "bst_training_pipeline.yaml")
    compiled = package_path.read_text(encoding="utf-8")

    assert package_path.name == "bst_training_pipeline.yaml"
    assert "/opt/venv/bin/python" in compiled
    assert "/opt/spark/bin/spark-submit" not in compiled
    assert "/opt/recsys/apps/ml-system/src/cli/prepare_bst_training_data.py" in compiled
    assert "--feature-source" in compiled
    assert "--offline-feature-table" in compiled
    assert "--hudi-enabled" in compiled
    assert "--dataset-metadata-path" in compiled
    assert "offline_feature_table" in compiled
    assert "recsys_features.feature_store.ml_bst_training" in compiled
    assert "training_table_path" not in compiled
    assert "/opt/recsys/apps/ml-system/src/cli/submit_ray_job.py" in compiled
    assert "--job-mode" in compiled
    assert "distributed-train" in compiled
    assert "recsys-bst-ray-ddp-train" in compiled
    assert "distributed_worker_replicas: int [Default: 2.0]" in compiled
    assert "distributed_num_workers: int [Default: 2.0]" in compiled
    assert "/opt/recsys/apps/ml-system/src/cli/evaluate_ray_best_bst.py" in compiled
    assert "/opt/recsys/apps/ml-system/src/registry/model_promotion.py" in compiled
    assert "/opt/recsys/apps/ml-system/src/cli/trigger_kserve_cd.py" in compiled
    assert "Trigger KServe CD" in compiled
    assert "kserve_cd_score_threshold" in compiled
    assert "RecSys-KServe-Model-CD" in compiled
    assert "pipelines.model_pipeline" not in compiled
    assert "recsys_model_pipeline" not in compiled
    validate_pipeline_package(
        package_path=package_path,
        required_images=[training_image, spark_image],
        forbidden_tokens=[":local"],
    )


def test_upload_pipeline_package_adds_version_when_pipeline_exists():
    class Client:
        def __init__(self):
            self.version_uploads = []

        def get_pipeline_id(self, name):
            assert name == "recsys-pipeline"
            return "pipeline-1"

        def upload_pipeline_version(self, **kwargs):
            self.version_uploads.append(kwargs)
            return type("Version", (), {"pipeline_version_id": "version-1"})()

    client = Client()

    result = upload_or_version_pipeline(
        client=client,
        package_path="pipeline.yaml",
        pipeline_name="recsys-pipeline",
        version_name="ci-abc-build-1",
        description="ci upload",
    )

    assert result["action"] == "uploaded_pipeline_version"
    assert result["pipeline_id"] == "pipeline-1"
    assert result["pipeline_version_id"] == "version-1"
    assert client.version_uploads == [
        {
            "pipeline_package_path": "pipeline.yaml",
            "pipeline_version_name": "ci-abc-build-1",
            "pipeline_id": "pipeline-1",
            "description": "ci upload",
        }
    ]


def test_upload_pipeline_package_creates_pipeline_when_missing():
    class Client:
        def get_pipeline_id(self, name):
            return None

        def upload_pipeline(self, **kwargs):
            self.upload = kwargs
            return type("Pipeline", (), {"pipeline_id": "pipeline-new"})()

    client = Client()

    result = upload_or_version_pipeline(
        client=client,
        package_path="pipeline.yaml",
        pipeline_name="recsys-pipeline",
        version_name="ci-abc-build-1",
        description="ci upload",
    )

    assert result["action"] == "uploaded_pipeline"
    assert result["pipeline_id"] == "pipeline-new"
    assert client.upload == {
        "pipeline_package_path": "pipeline.yaml",
        "pipeline_name": "recsys-pipeline",
        "description": "ci upload",
    }
