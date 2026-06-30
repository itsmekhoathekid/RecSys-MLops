import os

from kfp import dsl

from kubeflow.components.runtime import (
    DEFAULT_PVC_MOUNT_PATH,
    DEFAULT_PVC_NAME,
    DEFAULT_RUNTIME_SECRET_NAME,
    wire_runtime,
)


PIPELINE_IMAGE = os.getenv("RECSYS_PIPELINE_IMAGE", "recsys-mlops-training:local")
RAY_IMAGE = os.getenv("RECSYS_RAY_IMAGE", PIPELINE_IMAGE)
SPARK_IMAGE = os.getenv("RECSYS_SPARK_IMAGE", "recsys-mlops-spark:local")
SPARK_PACKAGES = os.getenv(
    "RECSYS_SPARK_PACKAGES",
    "org.apache.iceberg:iceberg-spark-runtime-3.5_2.12:1.9.2,"
    "org.apache.hudi:hudi-spark3.5-bundle_2.12:1.0.2,"
    "org.apache.hadoop:hadoop-aws:3.3.4,"
    "com.amazonaws:aws-java-sdk-bundle:1.12.262",
)


@dsl.container_component
def feature_engineering(config_path: str, output_base: str, run_path: str, summary_path: str):
    return dsl.ContainerSpec(
        image=PIPELINE_IMAGE,
        command=["python", "/opt/recsys/apps/ml-system/src/run_features.py"],
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
    offline_feature_table: str,
    output_dir: str,
    max_history_len: int,
    dataset_metadata_path: str,
    feature_service_name: str,
    iceberg_catalog_name: str,
    iceberg_warehouse: str,
    hudi_catalog_name: str,
    hudi_warehouse: str,
):
    return dsl.ContainerSpec(
        image=SPARK_IMAGE,
        command=["/opt/spark/bin/spark-submit"],
        args=[
            "--master",
            "local[*]",
            "--packages",
            SPARK_PACKAGES,
            "/opt/recsys/apps/ml-system/src/cli/prepare_bst_training_data.py",
            "--feature-source",
            "offline_feature_store",
            "--offline-feature-table",
            offline_feature_table,
            "--output-dir",
            output_dir,
            "--max-history-len",
            max_history_len,
            "--feature-service-name",
            feature_service_name,
            "--hudi-enabled",
            "true",
            "--hudi-catalog-name",
            hudi_catalog_name,
            "--hudi-warehouse",
            hudi_warehouse,
            "--iceberg-catalog-name",
            iceberg_catalog_name,
            "--iceberg-warehouse",
            iceberg_warehouse,
            "--dataset-metadata-path",
            dataset_metadata_path,
        ],
    )


@dsl.container_component
def submit_rayjob(
    pipeline_run_id: str,
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
    cpus_per_trial: float,
    gpus_per_trial: float,
    worker_replicas: int,
    head_ray_num_cpus: str,
    use_gpu: bool,
    gpu_limit: int,
    status_path: str,
    dataset_metadata_path: str,
):
    return dsl.ContainerSpec(
        image=PIPELINE_IMAGE,
        command=["python", "/opt/recsys/apps/ml-system/src/cli/submit_ray_job.py"],
        args=[
            "--pipeline-run-id",
            pipeline_run_id,
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
            "--cpus-per-trial",
            cpus_per_trial,
            "--gpus-per-trial",
            gpus_per_trial,
            "--worker-replicas",
            worker_replicas,
            "--head-ray-num-cpus",
            head_ray_num_cpus,
            "--use-gpu-value",
            use_gpu,
            "--gpu-limit",
            gpu_limit,
            "--status-path",
            status_path,
            "--dataset-metadata-path",
            dataset_metadata_path,
        ],
    )


@dsl.container_component
def evaluate_bst(config_path: str, ray_result_path: str, metrics_path: str, dataset_metadata_path: str):
    return dsl.ContainerSpec(
        image=PIPELINE_IMAGE,
        command=["python", "/opt/recsys/apps/ml-system/src/cli/evaluate_ray_best_bst.py"],
        args=[
            "--config-path",
            config_path,
            "--ray-result-path",
            ray_result_path,
            "--split",
            "test",
            "--metrics-path",
            metrics_path,
            "--dataset-metadata-path",
            dataset_metadata_path,
        ],
    )


@dsl.container_component
def promote_bst_model(
    config_path: str,
    ray_result_path: str,
    output_dir: str,
    manifest_path: str,
    metric_name: str,
):
    return dsl.ContainerSpec(
        image=PIPELINE_IMAGE,
        command=["python", "/opt/recsys/apps/ml-system/src/registry/model_promotion.py"],
        args=[
            "--config-path",
            config_path,
            "--ray-result-path",
            ray_result_path,
            "--output-dir",
            output_dir,
            "--manifest-path",
            manifest_path,
            "--metric-name",
            metric_name,
        ],
    )


@dsl.pipeline(
    name="recsys-bst-feature-train-evaluate",
    description="Feature engineering, BST training, evaluation, MLflow tracking, MinIO artifacts, and Postgres model config.",
)
def recsys_bst_pipeline(
    pipeline_run_id: str = "manual",
    config_path: str = "configs/local/spark_batch.yaml",
    bst_config_path: str = "configs/local/bst.yaml",
    source_run_path: str = "apps/data-platform/data-generator/src/output/test_10k_seed42",
    workspace_root: str = "/workspace/recsys",
    output_base: str = "/workspace/recsys/data_platform/output",
    feature_summary_path: str = "/workspace/recsys/data_platform/output/feature_summary.json",
    offline_feature_table: str = "recsys_features.feature_store.ml_bst_training",
    split_output_dir: str = "/workspace/recsys/data_platform/output/ml/bst_split",
    dataset_metadata_path: str = "/workspace/recsys/data_platform/output/ml/bst_split/dataset_version_meta.json",
    ray_output_dir: str = "/workspace/recsys/data_platform/output/ml/ray",
    ray_best_result_path: str = "/workspace/recsys/data_platform/output/ml/ray/best_result.json",
    ray_status_path: str = "/workspace/recsys/data_platform/output/ml/ray/rayjob_status.json",
    eval_metrics_path: str = "/workspace/recsys/data_platform/output/ml/eval_metrics.json",
    serving_output_dir: str = "/workspace/recsys/data_platform/output/ml/serving",
    promotion_manifest_path: str = "/workspace/recsys/data_platform/output/ml/serving/promotion_manifest.json",
    promotion_metric_name: str = "test_ndcg_at_10",
    pvc_name: str = "recsys-mlops-pvc",
    pvc_mount_path: str = "/workspace",
    runtime_secret_name: str = "recsys-mlops-runtime",
    ray_namespace: str = "kubeflow",
    ray_job_name: str = "recsys-bst-ray-tune",
    ray_image: str = RAY_IMAGE,
    feature_service_name: str = "bst_ranking_v1",
    iceberg_catalog_name: str = "recsys_features",
    iceberg_warehouse: str = "s3a://recsys-offline-feature-store/warehouse",
    hudi_catalog_name: str = "recsys_features",
    hudi_warehouse: str = "s3a://recsys-offline-feature-store/warehouse",
    max_history_len: int = 50,
    training_percent: float = 0.01,
    num_epochs: int = 1,
    max_trials: int = 2,
    parallel_trials: int = 1,
    cpus_per_trial: float = 1.0,
    gpus_per_trial: float = 0.0,
    worker_replicas: int = 1,
    head_ray_num_cpus: str = "0",
    use_gpu: bool = False,
    gpu_limit: int = 1,
):
    prepare = wire_runtime(
        prepare_training_data(
            offline_feature_table=offline_feature_table,
            output_dir=split_output_dir,
            max_history_len=max_history_len,
            dataset_metadata_path=dataset_metadata_path,
            feature_service_name=feature_service_name,
            iceberg_catalog_name=iceberg_catalog_name,
            iceberg_warehouse=iceberg_warehouse,
            hudi_catalog_name=hudi_catalog_name,
            hudi_warehouse=hudi_warehouse,
        ),
        pvc_name=DEFAULT_PVC_NAME,
        mount_path=DEFAULT_PVC_MOUNT_PATH,
        secret_name=DEFAULT_RUNTIME_SECRET_NAME,
    )
    tune_train = wire_runtime(
        submit_rayjob(
            pipeline_run_id=pipeline_run_id,
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
            cpus_per_trial=cpus_per_trial,
            gpus_per_trial=gpus_per_trial,
            worker_replicas=worker_replicas,
            head_ray_num_cpus=head_ray_num_cpus,
            use_gpu=use_gpu,
            gpu_limit=gpu_limit,
            status_path=ray_status_path,
            dataset_metadata_path=dataset_metadata_path,
        ),
        pvc_name=DEFAULT_PVC_NAME,
        mount_path=DEFAULT_PVC_MOUNT_PATH,
        secret_name=DEFAULT_RUNTIME_SECRET_NAME,
    ).after(prepare)
    evaluate = wire_runtime(
        evaluate_bst(
            config_path=bst_config_path,
            ray_result_path=ray_best_result_path,
            metrics_path=eval_metrics_path,
            dataset_metadata_path=dataset_metadata_path,
        ),
        pvc_name=DEFAULT_PVC_NAME,
        mount_path=DEFAULT_PVC_MOUNT_PATH,
        secret_name=DEFAULT_RUNTIME_SECRET_NAME,
    ).after(tune_train)
    wire_runtime(
        promote_bst_model(
            config_path=bst_config_path,
            ray_result_path=ray_best_result_path,
            output_dir=serving_output_dir,
            manifest_path=promotion_manifest_path,
            metric_name=promotion_metric_name,
        ),
        pvc_name=DEFAULT_PVC_NAME,
        mount_path=DEFAULT_PVC_MOUNT_PATH,
        secret_name=DEFAULT_RUNTIME_SECRET_NAME,
    ).after(evaluate)
