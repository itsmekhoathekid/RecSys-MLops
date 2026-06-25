from __future__ import annotations

import os

try:
    from airflow import DAG
    from airflow.operators.empty import EmptyOperator
    from airflow.providers.docker.operators.docker import DockerOperator
    from airflow.utils.task_group import TaskGroup
    from pendulum import datetime
except ImportError:  # pragma: no cover
    DAG = DockerOperator = EmptyOperator = TaskGroup = datetime = None


DOCKER_NETWORK = "recsys-dataflow_recsys-dataflow"
COMMON_ENV = {
    "PYTHONPATH": "/opt/recsys/apps/data-platform/src:/opt/recsys",
    "MINIO_ENDPOINT": "http://minio:9000",
    "AWS_ACCESS_KEY_ID": os.getenv("AWS_ACCESS_KEY_ID", os.getenv("MINIO_ROOT_USER", "")),
    "AWS_SECRET_ACCESS_KEY": os.getenv("AWS_SECRET_ACCESS_KEY", os.getenv("MINIO_ROOT_PASSWORD", "")),
    "AWS_DEFAULT_REGION": "us-east-1",
    "LAKE_BUCKET": "recsys-lakehouse",
    "OFFLINE_FEATURE_BUCKET": "recsys-offline-feature-store",
    "LAKEHOUSE_WAREHOUSE": "s3a://recsys-lakehouse/warehouse",
    "ICEBERG_CATALOG": "recsys",
    "ICEBERG_LAKEHOUSE_NAMESPACE": "lakehouse",
    "OFFLINE_FEATURE_CATALOG": "recsys_features",
    "OFFLINE_FEATURE_STORE_WAREHOUSE": "s3a://recsys-offline-feature-store/warehouse",
    "ICEBERG_FEATURE_NAMESPACE": "feature_store",
    "OFFLINE_FEATURE_STORE_URI": "s3a://recsys-offline-feature-store/warehouse/feature_store",
    "POSTGRES_HOST": "postgres",
    "POSTGRES_PORT": "5432",
    "POSTGRES_DB": "recsys",
    "POSTGRES_USER": "recsys",
    "POSTGRES_PASSWORD": os.getenv("POSTGRES_PASSWORD", ""),
    "KAFKA_BOOTSTRAP_SERVERS": "kafka:29092",
    "KAFKA_CONNECT_URL": "http://kafka-connect:8083",
    "REDIS_HOST": "redis",
    "REDIS_PORT": "6379",
    "OFFLINE_STORE_ENABLED": "true",
    "DATAFLOW_OUTPUT_MODE": "s3",
}


def docker_task(task_id: str, image: str, command: str):
    return DockerOperator(
        task_id=task_id,
        image=image,
        command=["bash", "-lc", f"{command} "],
        docker_url="unix://var/run/docker.sock",
        network_mode=DOCKER_NETWORK,
        auto_remove=True,
        mount_tmp_dir=False,
        environment=COMMON_ENV,
    )


def cli_task(task_id: str, command: str):
    return docker_task(task_id, "recsys-dataflow-cli:local", command)


def spark_task(task_id: str, command: str):
    return docker_task(task_id, "recsys-spark:local", command)


def flink_task(task_id: str, command: str):
    return docker_task(task_id, "recsys-flink:local", command)


if DAG is not None:
    with DAG(
        dag_id="full_dataflow_local_dag",
        start_date=datetime(2026, 1, 1),
        schedule=None,
        catchup=False,
        tags=["recsys", "native-lakehouse"],
    ) as dag:
        start = EmptyOperator(task_id="start")
        end = EmptyOperator(task_id="end")

        with TaskGroup("platform_init") as platform_init:
            init_postgres_schema = cli_task(
                "init_postgres_schema",
                "PYTHONPATH=/opt/recsys/apps/data-platform/data-generator/src:/opt/recsys "
                "python infra/docker/scripts/init_postgres_schema.py",
            )
            register_debezium = cli_task(
                "register_debezium_connector",
                "bash infra/docker/scripts/register_debezium_connector.sh",
            )
            init_postgres_schema >> register_debezium

        with TaskGroup("historical_batch_to_offline_store") as historical_batch_to_offline_store:
            generate_historical_raw = cli_task(
                "generate_historical_raw_files",
                "PYTHONPATH=/opt/recsys/apps/data-platform/data-generator/src:/opt/recsys "
                "python apps/data-platform/data-generator/src/scripts/generate_historical_to_minio.py "
                "--target s3 --bucket recsys-lakehouse --prefix raw",
            )
            ingest_historical_batch_to_lakehouse = spark_task(
                "ingest_historical_batch_to_lakehouse",
                "/opt/spark/bin/spark-submit "
                "apps/data-platform/src/ingest/batch_lakehouse_ingestion.py "
                "--run-path s3a://recsys-lakehouse/raw/test_10k_seed42 "
                "--mode overwrite",
            )
            run_historical_spark_batch = spark_task(
                "run_historical_spark_batch",
                "/opt/spark/bin/spark-submit "
                "apps/data-platform/src/feature_engineering/spark/spark_batch_entrypoint.py "
                "--config configs/local/spark_batch.yaml",
            )
            generate_historical_raw >> ingest_historical_batch_to_lakehouse >> run_historical_spark_batch

        with TaskGroup("realtime_cdc_to_feature_stores") as realtime_cdc_to_feature_stores:
            load_realtime_source = cli_task(
                "load_realtime_to_postgres",
                "PYTHONPATH=/opt/recsys/apps/data-platform/data-generator/src:/opt/recsys "
                "python apps/data-platform/data-generator/src/scripts/load_realtime_to_postgres.py --limit-per-table 200",
            )
            submit_pyflink_stream_job = flink_task(
                "submit_pyflink_stream_job",
                "PYTHONPATH=/opt/recsys/apps/data-platform/src:/opt/recsys "
                "flink run -m flink-jobmanager:8081 "
                "-py apps/data-platform/src/feature_engineering/flink/realtime_stream_job.py "
                "-- --runner pyflink --topic cdc.behavior_events --max-events 200 --min-events 1 "
                "--offline-store-enabled "
                "--offline-feature-catalog $OFFLINE_FEATURE_CATALOG "
                "--offline-feature-store-warehouse $OFFLINE_FEATURE_STORE_WAREHOUSE",
            )
            load_realtime_source >> submit_pyflink_stream_job

        start >> platform_init
        platform_init >> [historical_batch_to_offline_store, realtime_cdc_to_feature_stores]
        [historical_batch_to_offline_store, realtime_cdc_to_feature_stores] >> end
