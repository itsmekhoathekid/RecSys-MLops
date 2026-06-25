from __future__ import annotations

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
    "MINIO_ROOT_USER": "minio",
    "MINIO_ROOT_PASSWORD": "minio123",
    "AWS_ACCESS_KEY_ID": "minio",
    "AWS_SECRET_ACCESS_KEY": "minio123",
    "AWS_DEFAULT_REGION": "us-east-1",
    "AWS_ENDPOINT_URL": "http://minio:9000",
    "FEAST_S3_ENDPOINT": "http://minio:9000",
    "LAKE_BUCKET": "recsys-lake",
    "FEATURE_STORE_BUCKET": "recsys-feature-store",
    "POSTGRES_HOST": "postgres",
    "POSTGRES_PORT": "5432",
    "POSTGRES_DB": "recsys",
    "POSTGRES_USER": "recsys",
    "POSTGRES_PASSWORD": "recsys",
    "KAFKA_BOOTSTRAP_SERVERS": "kafka:29092",
    "KAFKA_CONNECT_URL": "http://kafka-connect:8083",
    "REDIS_HOST": "redis",
    "REDIS_PORT": "6379",
    "FEAST_OFFLINE_ROOT": "s3://recsys-feature-store/offline",
    "DATAFLOW_OUTPUT_MODE": "s3",
}


SPARK_SUBMIT_BASE = (
    "/opt/spark/bin/spark-submit --master spark://spark-master:7077 "
    "--packages org.apache.hadoop:hadoop-aws:3.3.4,com.amazonaws:aws-java-sdk-bundle:1.12.262 "
    "--conf spark.hadoop.fs.s3a.endpoint=http://minio:9000 "
    "--conf spark.hadoop.fs.s3a.access.key=minio "
    "--conf spark.hadoop.fs.s3a.secret.key=minio123 "
    "--conf spark.hadoop.fs.s3a.path.style.access=true "
    "--conf spark.hadoop.fs.s3a.connection.ssl.enabled=false "
)


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
        tags=["recsys", "full-dataflow-local"],
    ) as dag:
        start = EmptyOperator(task_id="start")
        end = EmptyOperator(task_id="end")

        with TaskGroup("platform_init") as platform_init:
            check_services = cli_task(
                "check_services",
                "python infra/docker/scripts/smoke_check_stack.py --phase services",
            )
            init_postgres_schema = cli_task(
                "init_postgres_schema",
                "PYTHONPATH=/opt/recsys/apps/data-platform/data-generator/src:/opt/recsys "
                "python infra/docker/scripts/init_postgres_schema.py",
            )
            register_debezium = cli_task(
                "register_debezium_connector",
                "bash infra/docker/scripts/register_debezium_connector.sh",
            )
            register_minio_sink = cli_task(
                "register_kafka_minio_sink",
                "bash infra/docker/scripts/register_minio_sink_connector.sh",
            )

            check_services >> init_postgres_schema >> register_debezium >> register_minio_sink

        with TaskGroup("historical_bootstrap_path") as historical_bootstrap_path:
            generate_historical_raw = cli_task(
                "generate_historical_to_lake_raw",
                "PYTHONPATH=/opt/recsys/apps/data-platform/data-generator/src:/opt/recsys "
                "python apps/data-platform/data-generator/src/scripts/generate_historical_to_minio.py "
                "--target s3 --bucket recsys-lake --prefix raw",
            )
            run_historical_spark_batch = spark_task(
                "run_historical_spark_batch",
                f"{SPARK_SUBMIT_BASE}"
                "apps/data-platform/src/feature_engineering/spark/spark_batch_entrypoint.py "
                "--config configs/local/spark_batch.yaml",
            )
            historical_feast_materialize = cli_task(
                "historical_feast_materialize",
                "PYTHONPATH=/opt/recsys/apps/data-platform/src:/opt/recsys/apps/data-platform/feature-store/src:/opt/recsys "
                "python apps/data-platform/feature-store/src/apply_feast_repo.py && "
                "PYTHONPATH=/opt/recsys/apps/data-platform/src:/opt/recsys/apps/data-platform/feature-store/src:/opt/recsys "
                "python apps/data-platform/feature-store/src/materialize_offline_to_online.py",
            )

            generate_historical_raw >> run_historical_spark_batch >> historical_feast_materialize

        with TaskGroup("realtime_cdc_path") as realtime_cdc_path:
            load_realtime_source = cli_task(
                "load_realtime_to_postgres",
                "PYTHONPATH=/opt/recsys/apps/data-platform/data-generator/src:/opt/recsys "
                "python apps/data-platform/data-generator/src/scripts/load_realtime_to_postgres.py --limit-per-table 200",
            )
            wait_for_cdc_to_bronze = cli_task(
                "wait_for_cdc_to_bronze",
                "python infra/docker/scripts/validate_bronze_cdc.py "
                "--topic cdc.behavior_events --min-records 1",
            )
            validate_bronze = cli_task(
                "validate_bronze_behavior_events",
                "python infra/docker/scripts/smoke_check_stack.py --phase bronze",
            )

            load_realtime_source >> wait_for_cdc_to_bronze >> validate_bronze

        with TaskGroup("realtime_batch_to_offline_path") as realtime_batch_to_offline_path:
            run_realtime_spark_batch = spark_task(
                "run_realtime_spark_bronze_batch",
                f"{SPARK_SUBMIT_BASE}"
                "apps/data-platform/src/feature_engineering/spark/spark_realtime_bronze_entrypoint.py "
                "--bronze-root s3://recsys-lake/bronze/kafka "
                "--offline-root s3://recsys-feature-store/offline "
                "--topic cdc.behavior_events",
            )
            realtime_feast_materialize = cli_task(
                "realtime_feast_materialize",
                "PYTHONPATH=/opt/recsys/apps/data-platform/src:/opt/recsys/apps/data-platform/feature-store/src:/opt/recsys "
                "python apps/data-platform/feature-store/src/apply_feast_repo.py && "
                "PYTHONPATH=/opt/recsys/apps/data-platform/src:/opt/recsys/apps/data-platform/feature-store/src:/opt/recsys "
                "python apps/data-platform/feature-store/src/materialize_offline_to_online.py",
            )
            validate_offline_features = cli_task(
                "validate_offline_feature_outputs",
                "python infra/docker/scripts/smoke_check_stack.py --phase offline",
            )

            run_realtime_spark_batch >> realtime_feast_materialize >> validate_offline_features

        with TaskGroup("realtime_stream_to_online_path") as realtime_stream_to_online_path:
            submit_pflink_stream_job = flink_task(
                "submit_pflink_stream_job",
                "PYTHONPATH=/opt/recsys/apps/data-platform/src:/opt/recsys "
                "flink run -m flink-jobmanager:8081 "
                "-py apps/data-platform/src/feature_engineering/flink/realtime_stream_job.py "
                "-- --runner pyflink --topic cdc.behavior_events --max-events 200 --min-events 1",
            )
            validate_streaming_redis = cli_task(
                "validate_streaming_redis_keys",
                "python infra/docker/scripts/smoke_check_stack.py --phase redis",
            )

            submit_pflink_stream_job >> validate_streaming_redis

        with TaskGroup("final_validation") as final_validation:
            final_smoke_check = cli_task(
                "final_smoke_check",
                "python infra/docker/scripts/smoke_check_stack.py --phase all",
            )

        start >> platform_init
        platform_init >> [historical_bootstrap_path, realtime_cdc_path]
        [historical_bootstrap_path, realtime_cdc_path] >> realtime_batch_to_offline_path
        realtime_cdc_path >> realtime_stream_to_online_path
        [
            historical_bootstrap_path,
            realtime_batch_to_offline_path,
            realtime_stream_to_online_path,
        ] >> final_validation
        final_validation >> end
