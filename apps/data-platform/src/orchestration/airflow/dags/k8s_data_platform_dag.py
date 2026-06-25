from __future__ import annotations

try:
    from airflow import DAG
    from airflow.operators.empty import EmptyOperator
    from airflow.providers.cncf.kubernetes.operators.pod import KubernetesPodOperator
    from kubernetes.client import models as k8s
    from pendulum import datetime
except ImportError:  # pragma: no cover
    DAG = EmptyOperator = KubernetesPodOperator = datetime = k8s = None


NAMESPACE = "recsys-dataflow"
DATAFLOW_IMAGE = "recsys-dataflow-cli:local"
FLINK_IMAGE = "recsys-flink:local"
SPARK_IMAGE = "recsys-spark:local"
COMMON_ENV = {
    "PYTHONPATH": "/opt/recsys/apps/data-platform/src:/opt/recsys",
}


def pod_env_from():
    if k8s is None:
        return []
    return [
        k8s.V1EnvFromSource(config_map_ref=k8s.V1ConfigMapEnvSource(name="recsys-data-platform-config")),
        k8s.V1EnvFromSource(secret_ref=k8s.V1SecretEnvSource(name="recsys-data-platform-secret")),
    ]


def pod_task(task_id: str, image: str, command: str):
    return KubernetesPodOperator(
        task_id=task_id,
        name=task_id.replace("_", "-"),
        namespace=NAMESPACE,
        image=image,
        cmds=["bash", "-lc"],
        arguments=[command],
        env_vars=COMMON_ENV,
        env_from=pod_env_from(),
        annotations={"sidecar.istio.io/inject": "false"},
        get_logs=True,
        is_delete_operator_pod=True,
        in_cluster=True,
    )


if DAG is not None:
    with DAG(
        dag_id="k8s_data_platform_dag",
        start_date=datetime(2026, 1, 1),
        schedule=None,
        catchup=False,
        tags=["recsys", "k8s", "native-lakehouse"],
    ) as dag:
        start = EmptyOperator(task_id="start")
        end = EmptyOperator(task_id="end")

        init_data_platform_minio = pod_task(
            "init_data_platform_minio",
            DATAFLOW_IMAGE,
            "python -m ingest.init_data_platform_minio",
        )
        init_source_schema = pod_task(
            "init_source_schema",
            DATAFLOW_IMAGE,
            "PYTHONPATH=/opt/recsys/apps/data-platform/data-generator/src:/opt/recsys "
            "python infra/docker/scripts/init_postgres_schema.py",
        )
        register_debezium_connector = pod_task(
            "register_debezium_connector",
            DATAFLOW_IMAGE,
            "python -m ingest.register_k8s_connectors --connector debezium",
        )
        generate_historical_raw_files = pod_task(
            "generate_historical_raw_files",
            DATAFLOW_IMAGE,
            "PYTHONPATH=/opt/recsys/apps/data-platform/data-generator/src:/opt/recsys "
            "python apps/data-platform/data-generator/src/scripts/generate_historical_to_minio.py "
            "--target s3 --bucket recsys-lakehouse --prefix raw",
        )
        ingest_historical_batch_to_lakehouse = pod_task(
            "ingest_historical_batch_to_lakehouse",
            SPARK_IMAGE,
            "/opt/spark/bin/spark-submit "
            "apps/data-platform/src/ingest/batch_lakehouse_ingestion.py "
            "--run-path s3a://recsys-lakehouse/raw/test_10k_seed42 "
            "--mode overwrite",
        )
        load_realtime_to_source_postgres = pod_task(
            "load_realtime_to_source_postgres",
            DATAFLOW_IMAGE,
            "PYTHONPATH=/opt/recsys/apps/data-platform/data-generator/src:/opt/recsys "
            "python apps/data-platform/data-generator/src/scripts/load_realtime_to_postgres.py "
            "--limit-per-table 200",
        )
        run_spark_batch_to_offline_store = pod_task(
            "run_spark_batch_to_offline_store",
            SPARK_IMAGE,
            "/opt/spark/bin/spark-submit "
            "apps/data-platform/src/feature_engineering/spark/spark_batch_entrypoint.py "
            "--config configs/local/spark_batch.yaml",
        )
        run_flink_stream_to_feature_stores = pod_task(
            "run_flink_stream_to_feature_stores",
            FLINK_IMAGE,
            "flink run -m flink-jobmanager:8081 "
            "-py apps/data-platform/src/feature_engineering/flink/realtime_stream_job.py "
            "-- "
            "--runner pyflink --topic cdc.behavior_events --max-events 200 --min-events 1 "
            "--offline-store-enabled "
            "--offline-feature-catalog $OFFLINE_FEATURE_CATALOG "
            "--offline-feature-store-warehouse $OFFLINE_FEATURE_STORE_WAREHOUSE",
        )
        offline_feature_drift = pod_task(
            "offline_feature_drift",
            SPARK_IMAGE,
            "/opt/spark/bin/spark-submit "
            "apps/data-platform/src/validate/offline_feature_drift.py "
            "--report-path $OFFLINE_FEATURE_DRIFT_REPORT_PATH "
            "--threshold $RETRAIN_PSI_THRESHOLD "
            "--pushgateway-url $PUSHGATEWAY_URL",
        )
        trigger_kubeflow_retrain = pod_task(
            "trigger_kubeflow_retrain",
            DATAFLOW_IMAGE,
            "python -m mlops.trigger_kubeflow_retrain "
            "--drift-report-path $OFFLINE_FEATURE_DRIFT_REPORT_PATH "
            "--kfp-endpoint $KFP_ENDPOINT "
            "--experiment-name $KFP_EXPERIMENT_NAME "
            "--pipeline-package-path $KFP_PIPELINE_PACKAGE_PATH "
            "--pushgateway-url $PUSHGATEWAY_URL "
            "--pipeline-arg source_run_path=s3a://recsys-lakehouse/raw/test_10k_seed42",
        )
        datahub_ingest = pod_task(
            "datahub_ingest",
            DATAFLOW_IMAGE,
            "python -m metadata.ingest_datahub_governance "
            "--gms-url $DATAHUB_GMS_URL "
            "--pushgateway-url $PUSHGATEWAY_URL",
        )

        (
            start
            >> init_data_platform_minio
            >> init_source_schema
            >> register_debezium_connector
            >> [generate_historical_raw_files, load_realtime_to_source_postgres]
        )
        generate_historical_raw_files >> ingest_historical_batch_to_lakehouse >> run_spark_batch_to_offline_store
        load_realtime_to_source_postgres >> run_flink_stream_to_feature_stores
        run_spark_batch_to_offline_store >> offline_feature_drift >> trigger_kubeflow_retrain
        [trigger_kubeflow_retrain, run_flink_stream_to_feature_stores] >> datahub_ingest >> end
