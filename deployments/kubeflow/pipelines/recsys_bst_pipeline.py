import os

from kfp import compiler, dsl
from kfp import kubernetes


PIPELINE_IMAGE = os.getenv("RECSYS_PIPELINE_IMAGE", "recsys-mlops-training:local")
KFP_PVC_NAME = os.getenv("RECSYS_KFP_PVC_NAME", "recsys-mlops-pvc")
KFP_PVC_MOUNT_PATH = os.getenv("RECSYS_KFP_PVC_MOUNT_PATH", "/workspace")
KFP_RUNTIME_SECRET_NAME = os.getenv("RECSYS_KFP_RUNTIME_SECRET_NAME", "recsys-mlops-runtime")


def _wire_runtime(task, pvc_name: str, mount_path: str, secret_name: str):
    kubernetes.mount_pvc(task, pvc_name=pvc_name, mount_path=mount_path)
    kubernetes.use_secret_as_env(
        task,
        secret_name=secret_name,
        secret_key_to_env={
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
        },
    )
    task.set_caching_options(False)
    return task


@dsl.container_component
def feature_engineering(config_path: str, output_base: str, run_path: str, summary_path: str):
    return dsl.ContainerSpec(
        image=PIPELINE_IMAGE,
        command=["python", "-m", "pipelines.model_pipeline.run_feature_engineering"],
        args=[
            "--source-config",
            config_path,
            "--output-base",
            output_base,
            "--run-path",
            run_path,
            "--summary-path",
            summary_path,
        ],
    )


@dsl.container_component
def prepare_training_data(
    training_table_path: str,
    output_dir: str,
    max_history_len: int,
):
    return dsl.ContainerSpec(
        image=PIPELINE_IMAGE,
        command=["python", "-m", "pipelines.model_pipeline.prepare_bst_training_data"],
        args=[
            "--input-path",
            training_table_path,
            "--output-dir",
            output_dir,
            "--max-history-len",
            max_history_len,
        ],
    )


@dsl.container_component
def submit_rayjob(
    namespace: str,
    job_name: str,
    image: str,
    pvc_name: str,
    runtime_secret_name: str,
    split_dir: str,
    ray_output_dir: str,
    best_result_path: str,
    training_percent: float,
    num_epochs: int,
    max_trials: int,
    parallel_trials: int,
    use_gpu: bool,
    gpu_limit: int,
    status_path: str,
):
    return dsl.ContainerSpec(
        image=PIPELINE_IMAGE,
        command=["python", "-m", "pipelines.model_pipeline.submit_ray_job"],
        args=[
            "--namespace",
            namespace,
            "--job-name",
            job_name,
            "--image",
            image,
            "--pvc-name",
            pvc_name,
            "--runtime-secret-name",
            runtime_secret_name,
            "--split-dir",
            split_dir,
            "--ray-output-dir",
            ray_output_dir,
            "--best-result-path",
            best_result_path,
            "--training-percent",
            training_percent,
            "--num-epochs",
            num_epochs,
            "--max-trials",
            max_trials,
            "--parallel-trials",
            parallel_trials,
            "--use-gpu-value",
            use_gpu,
            "--gpu-limit",
            gpu_limit,
            "--status-path",
            status_path,
        ],
    )


@dsl.container_component
def evaluate_bst(config_path: str, ray_result_path: str, metrics_path: str):
    return dsl.ContainerSpec(
        image=PIPELINE_IMAGE,
        command=["python", "-m", "pipelines.model_pipeline.evaluate_ray_best_bst"],
        args=[
            "--config-path",
            config_path,
            "--ray-result-path",
            ray_result_path,
            "--split",
            "test",
            "--metrics-path",
            metrics_path,
        ],
    )


@dsl.pipeline(
    name="recsys-bst-feature-train-evaluate",
    description="Feature engineering, BST training, evaluation, MLflow tracking, MinIO artifacts, and Postgres model config.",
)
def recsys_bst_pipeline(
    config_path: str = "config/spark_batch.yaml",
    bst_config_path: str = "config/bst.yaml",
    source_run_path: str = "data_generator/output/test_10k_seed42",
    workspace_root: str = "/workspace/recsys",
    output_base: str = "/workspace/recsys/data_pipeline/output",
    feature_summary_path: str = "/workspace/recsys/data_pipeline/output/feature_summary.json",
    training_table_path: str = "/workspace/recsys/data_pipeline/output/ml/offline/ml_bst_training",
    split_output_dir: str = "/workspace/recsys/notebooks/data/bst_split",
    ray_output_dir: str = "/workspace/recsys/data_pipeline/output/ml/ray",
    ray_best_result_path: str = "/workspace/recsys/data_pipeline/output/ml/ray/best_result.json",
    ray_status_path: str = "/workspace/recsys/data_pipeline/output/ml/ray/rayjob_status.json",
    eval_metrics_path: str = "/workspace/recsys/data_pipeline/output/ml/eval_metrics.json",
    pvc_name: str = "recsys-mlops-pvc",
    pvc_mount_path: str = "/workspace",
    runtime_secret_name: str = "recsys-mlops-runtime",
    ray_namespace: str = "kubeflow",
    ray_job_name: str = "recsys-bst-ray-tune",
    ray_image: str = "recsys-mlops-training:local",
    max_history_len: int = 50,
    training_percent: float = 0.01,
    num_epochs: int = 1,
    max_trials: int = 2,
    parallel_trials: int = 1,
    use_gpu: bool = False,
    gpu_limit: int = 1,
):
    features = _wire_runtime(
        feature_engineering(
            config_path=config_path,
            output_base=output_base,
            run_path=source_run_path,
            summary_path=feature_summary_path,
        ),
        pvc_name=KFP_PVC_NAME,
        mount_path=KFP_PVC_MOUNT_PATH,
        secret_name=KFP_RUNTIME_SECRET_NAME,
    )
    prepare = _wire_runtime(
        prepare_training_data(
            training_table_path=training_table_path,
            output_dir=split_output_dir,
            max_history_len=max_history_len,
        ),
        pvc_name=KFP_PVC_NAME,
        mount_path=KFP_PVC_MOUNT_PATH,
        secret_name=KFP_RUNTIME_SECRET_NAME,
    ).after(features)
    tune_train = _wire_runtime(
        submit_rayjob(
            namespace=ray_namespace,
            job_name=ray_job_name,
            image=ray_image,
            pvc_name=pvc_name,
            runtime_secret_name=runtime_secret_name,
            split_dir=split_output_dir,
            ray_output_dir=ray_output_dir,
            best_result_path=ray_best_result_path,
            training_percent=training_percent,
            num_epochs=num_epochs,
            max_trials=max_trials,
            parallel_trials=parallel_trials,
            use_gpu=use_gpu,
            gpu_limit=gpu_limit,
            status_path=ray_status_path,
        ),
        pvc_name=KFP_PVC_NAME,
        mount_path=KFP_PVC_MOUNT_PATH,
        secret_name=KFP_RUNTIME_SECRET_NAME,
    ).after(prepare)
    _wire_runtime(
        evaluate_bst(
            config_path=bst_config_path,
            ray_result_path=ray_best_result_path,
            metrics_path=eval_metrics_path,
        ),
        pvc_name=KFP_PVC_NAME,
        mount_path=KFP_PVC_MOUNT_PATH,
        secret_name=KFP_RUNTIME_SECRET_NAME,
    ).after(tune_train)


if __name__ == "__main__":
    compiler.Compiler().compile(
        pipeline_func=recsys_bst_pipeline,
        package_path="deployments/kubeflow/pipelines/recsys_bst_pipeline.yaml",
    )
