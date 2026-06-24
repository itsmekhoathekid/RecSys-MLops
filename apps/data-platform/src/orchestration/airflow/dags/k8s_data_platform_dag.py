from __future__ import annotations

try:
    from airflow import DAG
    from airflow.operators.empty import EmptyOperator
    from airflow.providers.cncf.kubernetes.operators.pod import KubernetesPodOperator
    from pendulum import datetime
except ImportError:  # pragma: no cover
    DAG = EmptyOperator = KubernetesPodOperator = datetime = None


NAMESPACE = "recsys-dataflow"
DATAFLOW_IMAGE = "recsys-dataflow-cli:local"
FLINK_IMAGE = "recsys-flink:local"
SPARK_IMAGE = "recsys-spark:local"
COMMON_ENV = {
    "PYTHONPATH": "/opt/recsys/apps/data-platform/src:/opt/recsys/apps/data-platform/feature-store/src:/opt/recsys",
    "DATA_PLATFORM_MINIO_ENDPOINT": "http://data-platform-minio:9000",
    "DATA_PLATFORM_MINIO_ROOT_USER": "minio",
    "DATA_PLATFORM_MINIO_ROOT_PASSWORD": "minio123",
    "MINIO_ENDPOINT": "http://data-platform-minio:9000",
    "MINIO_ROOT_USER": "minio",
    "MINIO_ROOT_PASSWORD": "minio123",
    "AWS_ACCESS_KEY_ID": "minio",
    "AWS_SECRET_ACCESS_KEY": "minio123",
    "AWS_DEFAULT_REGION": "us-east-1",
    "LAKE_BUCKET": "recsys-lake",
    "FEATURE_STORE_BUCKET": "recsys-feature-store",
    "POSTGRES_HOST": "source-postgres",
    "POSTGRES_PORT": "5432",
    "POSTGRES_DB": "recsys",
    "POSTGRES_USER": "recsys",
    "POSTGRES_PASSWORD": "recsys",
    "WAREHOUSE_POSTGRES_HOST": "warehouse-postgres",
    "WAREHOUSE_POSTGRES_PORT": "5432",
    "WAREHOUSE_POSTGRES_DB": "recsys_warehouse",
    "WAREHOUSE_POSTGRES_USER": "recsys",
    "WAREHOUSE_POSTGRES_PASSWORD": "recsys",
    "KAFKA_BOOTSTRAP_SERVERS": "kafka:29092",
    "REDIS_HOST": "redis",
    "REDIS_PORT": "6379",
    "FEAST_OFFLINE_ROOT": "s3://recsys-feature-store/offline",
    "FEAST_REPO_PATH": "/opt/recsys/apps/data-platform/feature-store/feature_repo",
    "FEAST_REGISTRY_BACKUP_URI": "s3://recsys-feature-store/registry/registry.db",
    "ML_ARTIFACT_ROOT": "s3://recsys-lake/silver/ml",
    "WAREHOUSE_ENABLED": "true",
    "DATAHUB_GMS_URL": "http://datahub-datahub-gms.datahub.svc.cluster.local:8080",
    "PUSHGATEWAY_URL": "http://recsys-pushgateway.observability.svc.cluster.local:9091",
    "OFFLINE_FEATURE_DRIFT_REPORT_PATH": "s3://recsys-lake/monitoring/offline_feature_drift/report.json",
    "KFP_ENDPOINT": "http://ml-pipeline.kubeflow.svc.cluster.local:8888",
    "KFP_EXPERIMENT_NAME": "recsys-observability-retrain",
    "KFP_PIPELINE_PACKAGE_PATH": "/opt/recsys/infra/kubeflow/compiled/bst_training_pipeline.yaml",
    "RETRAIN_ON_DRIFT": "true",
    "RETRAIN_PSI_THRESHOLD": "0.15",
    "RECSYS_JSON_LOGS": "1",
}


def pod_task(task_id: str, image: str, command: str):
    return KubernetesPodOperator(
        task_id=task_id,
        name=task_id.replace("_", "-"),
        namespace=NAMESPACE,
        image=image,
        cmds=["bash", "-lc"],
        arguments=[command],
        env_vars=COMMON_ENV,
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
        tags=["recsys", "k8s", "data-platform"],
    ) as dag:
        start = EmptyOperator(task_id="start")
        end = EmptyOperator(task_id="end")

        init_data_platform_minio = pod_task(
            "init_data_platform_minio",
            DATAFLOW_IMAGE,
            "python -m ingest.init_data_platform_minio",
        )
        init_warehouse = pod_task(
            "init_warehouse",
            DATAFLOW_IMAGE,
            "python -m warehouse.init_warehouse",
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
        register_kafka_minio_sink = pod_task(
            "register_kafka_minio_sink",
            DATAFLOW_IMAGE,
            "python -m ingest.register_k8s_connectors --connector s3-sink",
        )
        generate_historical_to_lake_raw = pod_task(
            "generate_historical_to_lake_raw",
            DATAFLOW_IMAGE,
            "PYTHONPATH=/opt/recsys/apps/data-platform/data-generator/src:/opt/recsys "
            "python apps/data-platform/data-generator/src/scripts/generate_historical_to_minio.py "
            "--target s3 --bucket recsys-lake --prefix raw",
        )
        load_realtime_to_source_postgres = pod_task(
            "load_realtime_to_source_postgres",
            DATAFLOW_IMAGE,
            "PYTHONPATH=/opt/recsys/apps/data-platform/data-generator/src:/opt/recsys "
            "python apps/data-platform/data-generator/src/scripts/load_realtime_to_postgres.py "
            "--limit-per-table 200",
        )
        wait_for_cdc_to_bronze = pod_task(
            "wait_for_cdc_to_bronze",
            DATAFLOW_IMAGE,
            "python infra/docker/scripts/validate_bronze_cdc.py "
            "--topic cdc.behavior_events --min-records 1 --timeout-seconds 240 --poll-seconds 10",
        )
        ingest_historical_to_staging = pod_task(
            "ingest_historical_to_staging",
            DATAFLOW_IMAGE,
            "python -m warehouse.historical_loader --run-path s3://recsys-lake/raw/test_10k_seed42",
        )
        run_flink_processing = pod_task(
            "run_flink_processing",
            FLINK_IMAGE,
            "python3 apps/data-platform/src/feature_engineering/flink/realtime_stream_job.py "
            "--topic cdc.behavior_events --max-events 200 --min-events 1 "
            "--warehouse-enabled --idle-timeout-seconds 60",
        )
        ge_validate_staging = pod_task(
            "ge_validate_staging",
            DATAFLOW_IMAGE,
            "python -m validate.great_expectations_runner "
            "--report-path s3://recsys-lake/monitoring/great_expectations/staging_validation.json",
        )
        dbt_transform_production = pod_task(
            "dbt_transform_production",
            DATAFLOW_IMAGE,
            "cd apps/data-platform/dbt/recsys_warehouse && "
            "dbt deps --profiles-dir . || true && "
            "dbt build --profiles-dir .",
        )
        write_offline_feature_store = pod_task(
            "write_offline_feature_store",
            SPARK_IMAGE,
            "python3 -m local.run_offline_features_from_warehouse",
        )
        validate_offline_feature_store = pod_task(
            "validate_offline_feature_store",
            DATAFLOW_IMAGE,
            "python apps/data-platform/feature-store/src/validate_feature_store.py",
        )
        sync_offline_to_online_store = pod_task(
            "sync_offline_to_online_store",
            DATAFLOW_IMAGE,
            "python apps/data-platform/feature-store/src/materialize_offline_to_online.py",
        )
        evidently_feature_drift = pod_task(
            "evidently_feature_drift",
            DATAFLOW_IMAGE,
            "python -m validate.offline_feature_drift "
            "--report-path $OFFLINE_FEATURE_DRIFT_REPORT_PATH "
            "--offline-root $FEAST_OFFLINE_ROOT "
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
            "--pushgateway-url $PUSHGATEWAY_URL",
        )
        datahub_ingest = pod_task(
            "datahub_ingest",
            DATAFLOW_IMAGE,
            "python -m metadata.ingest_datahub_governance --gms-url $DATAHUB_GMS_URL",
        )
        final_smoke = pod_task(
            "final_smoke",
            DATAFLOW_IMAGE,
            "python infra/docker/scripts/smoke_check_stack.py --phase offline",
        )

        (
            start
            >> init_data_platform_minio
            >> init_source_schema
            >> init_warehouse
            >> register_debezium_connector
            >> register_kafka_minio_sink
            >> generate_historical_to_lake_raw
            >> load_realtime_to_source_postgres
            >> wait_for_cdc_to_bronze
            >> ingest_historical_to_staging
            >> run_flink_processing
            >> ge_validate_staging
            >> dbt_transform_production
            >> write_offline_feature_store
            >> validate_offline_feature_store
            >> sync_offline_to_online_store
            >> evidently_feature_drift
            >> trigger_kubeflow_retrain
            >> datahub_ingest
            >> final_smoke
            >> end
        )
