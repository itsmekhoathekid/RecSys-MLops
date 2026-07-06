from __future__ import annotations

import os

try:
    from airflow import DAG
    from airflow.operators.empty import EmptyOperator
    from airflow.providers.cncf.kubernetes.operators.pod import KubernetesPodOperator
    from kubernetes.client import models as k8s
    from pendulum import datetime
except ImportError:  # pragma: no cover
    DAG = EmptyOperator = KubernetesPodOperator = datetime = k8s = None


NAMESPACE = "recsys-dataflow"
DATAFLOW_IMAGE = os.getenv("DATAFLOW_IMAGE", "recsys-dataflow-cli:local")
FLINK_IMAGE = os.getenv("FLINK_IMAGE", "recsys-flink:local")
SPARK_IMAGE = os.getenv("SPARK_IMAGE", os.getenv("SPARK_K8S_IMAGE", "recsys-spark:local"))
DATAFLOW_NODE_SELECTOR = os.getenv("DATAFLOW_NODE_SELECTOR", "recsys.ai/pool=cpu-services")
COMMON_ENV = {
    "PYTHONPATH": "/opt/recsys/apps/data-platform/src:/opt/recsys",
}
SPARK_DRIVER_EXECUTOR_ENV = (
    "PYTHONPATH",
    "DATA_PLATFORM_MINIO_ENDPOINT",
    "MINIO_ENDPOINT",
    "AWS_DEFAULT_REGION",
    "LAKE_BUCKET",
    "OFFLINE_FEATURE_BUCKET",
    "LAKEHOUSE_WAREHOUSE",
    "ICEBERG_CATALOG",
    "ICEBERG_LAKEHOUSE_NAMESPACE",
    "OFFLINE_FEATURE_CATALOG",
    "OFFLINE_FEATURE_STORE_WAREHOUSE",
    "ICEBERG_FEATURE_NAMESPACE",
    "OFFLINE_FEATURE_STORE_URI",
    "FEAST_POSTGRES_HOST",
    "FEAST_POSTGRES_PORT",
    "FEAST_POSTGRES_DB",
    "FEAST_POSTGRES_SCHEMA",
    "FEAST_POSTGRES_USER",
    "FEAST_POSTGRES_PASSWORD",
    "FEAST_POSTGRES_SSLMODE",
    "FEAST_POSTGRES_EXPORT_ENABLED",
    "OFFLINE_FEATURE_DRIFT_REPORT_PATH",
    "PUSHGATEWAY_URL",
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "RETRAIN_PSI_THRESHOLD",
    "RECSYS_JSON_LOGS",
    "SPARK_SQL_SHUFFLE_PARTITIONS",
)
SPARK_SECRET_ENV = ("MINIO_ROOT_USER", "MINIO_ROOT_PASSWORD", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY")


def dag_schedule():
    schedule = os.getenv("DATA_PLATFORM_DAG_SCHEDULE", "@daily")
    if schedule.lower() in {"", "none", "manual"}:
        return None
    return schedule


def pod_env_from():
    if k8s is None:
        return []
    return [
        k8s.V1EnvFromSource(config_map_ref=k8s.V1ConfigMapEnvSource(name="recsys-data-platform-config")),
        k8s.V1EnvFromSource(secret_ref=k8s.V1SecretEnvSource(name="recsys-data-platform-secret")),
    ]


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


def mesh_safe_command(command: str) -> str:
    quit_sidecar = (
        "python -c \"import urllib.request; "
        "req=urllib.request.Request('http://127.0.0.1:15020/quitquitquit', method='POST'); "
        "urllib.request.urlopen(req, timeout=2).read()\" >/dev/null 2>&1 || true"
    )
    return (
        "set -e; "
        f"cleanup() {{ status=$?; {quit_sidecar}; exit $status; }}; "
        "trap cleanup EXIT; "
        f"{command}"
    )


def optional_command(flag_name: str, command: str, label: str) -> str:
    return (
        f'if [ "${{{flag_name}:-true}}" = "true" ]; then '
        f"{command}; "
        "else "
        f'echo "Skipping {label} because {flag_name}=${{{flag_name}:-true}}"; '
        "fi"
    )


def pod_task(task_id: str, image: str, command: str, *, mesh: bool = True):
    annotations = {"sidecar.istio.io/inject": "false"}
    if mesh:
        annotations = {
            "proxy.istio.io/config": '{"holdApplicationUntilProxyStarts": true}',
            "sidecar.istio.io/inject": "true",
        }
    return KubernetesPodOperator(
        task_id=task_id,
        name=task_id.replace("_", "-"),
        namespace=NAMESPACE,
        image=image,
        cmds=["bash", "-c"],
        arguments=[mesh_safe_command(command)],
        env_vars=COMMON_ENV,
        env_from=pod_env_from(),
        annotations=annotations,
        node_selector=parse_node_selector(DATAFLOW_NODE_SELECTOR),
        get_logs=True,
        is_delete_operator_pod=True,
        in_cluster=True,
        startup_timeout_seconds=600,
    )


def spark_native_submit(task_id: str, application: str, application_args: str = "") -> str:
    app_name = f"recsys-{task_id.replace('_', '-')}".replace("run-spark-batch-to-offline-store", "spark-batch")
    env_conf = " ".join(
        f"--conf spark.kubernetes.driverEnv.{name}=${{{name}:-}} "
        f"--conf spark.executorEnv.{name}=${{{name}:-}}"
        for name in SPARK_DRIVER_EXECUTOR_ENV
    )
    secret_conf = " ".join(
        f"--conf spark.kubernetes.driver.secretKeyRef.{name}=recsys-data-platform-secret:{name} "
        f"--conf spark.kubernetes.executor.secretKeyRef.{name}=recsys-data-platform-secret:{name}"
        for name in SPARK_SECRET_ENV
    )
    return (
        'SPARK_APP_SUFFIX="$(date +%s)-${RANDOM}"; '
        "/opt/spark/bin/spark-submit "
        "--master ${SPARK_K8S_MASTER:-k8s://https://kubernetes.default.svc} "
        "--deploy-mode cluster "
        f"--name {app_name}-${{SPARK_APP_SUFFIX}} "
        "--conf spark.kubernetes.namespace=${SPARK_K8S_NAMESPACE:-recsys-dataflow} "
        "--conf spark.kubernetes.container.image=${SPARK_K8S_IMAGE:-recsys-spark:local} "
        "--conf spark.kubernetes.container.image.pullPolicy=${SPARK_K8S_IMAGE_PULL_POLICY:-IfNotPresent} "
        "--conf spark.kubernetes.authenticate.driver.serviceAccountName=${SPARK_K8S_SERVICE_ACCOUNT:-default} "
        "--conf spark.kubernetes.submission.waitAppCompletion=true "
        "--conf spark.kubernetes.submission.connectionTimeout=${SPARK_K8S_CONNECTION_TIMEOUT:-60000} "
        "--conf spark.kubernetes.submission.requestTimeout=${SPARK_K8S_REQUEST_TIMEOUT:-180000} "
        "--conf spark.kubernetes.report.interval=5s "
        "--conf spark.kubernetes.driver.annotation.sidecar.istio.io/inject=false "
        "--conf spark.kubernetes.executor.annotation.sidecar.istio.io/inject=false "
        "--conf spark.kubernetes.node.selector.recsys.ai/pool=${SPARK_K8S_NODE_POOL:-cpu-services} "
        "--conf spark.driver.memory=${SPARK_K8S_DRIVER_MEMORY:-1g} "
        "--conf spark.driver.memoryOverhead=${SPARK_K8S_DRIVER_MEMORY_OVERHEAD:-384m} "
        "--conf spark.driver.cores=${SPARK_K8S_DRIVER_CORES:-1} "
        "--conf spark.kubernetes.driver.request.cores=${SPARK_K8S_DRIVER_REQUEST_CORES:-500m} "
        "--conf spark.executor.instances=${SPARK_K8S_EXECUTOR_INSTANCES:-1} "
        "--conf spark.executor.memory=${SPARK_K8S_EXECUTOR_MEMORY:-1g} "
        "--conf spark.executor.memoryOverhead=${SPARK_K8S_EXECUTOR_MEMORY_OVERHEAD:-384m} "
        "--conf spark.executor.cores=${SPARK_K8S_EXECUTOR_CORES:-1} "
        "--conf spark.kubernetes.executor.request.cores=${SPARK_K8S_EXECUTOR_REQUEST_CORES:-500m} "
        f"{env_conf} "
        f"{secret_conf} "
        f"{application} {application_args}".strip()
    )


if DAG is not None:
    with DAG(
        dag_id="k8s_data_platform_dag",
        start_date=datetime(2026, 1, 1),
        schedule=dag_schedule(),
        catchup=False,
        tags=["recsys", "k8s", "native-lakehouse"],
    ) as dag:
        start = EmptyOperator(task_id="start")
        end = EmptyOperator(task_id="end")

        init_data_platform_minio = pod_task(
            "init_data_platform_minio",
            DATAFLOW_IMAGE,
            "python -m ingest.init_data_platform_minio",
            mesh=False,
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
            optional_command(
                "REALTIME_E2E_ENABLED",
                "python -m ingest.register_k8s_connectors --connector debezium",
                "Debezium connector registration",
            ),
        )
        generate_historical_raw_files = pod_task(
            "generate_historical_raw_files",
            DATAFLOW_IMAGE,
            "PYTHONPATH=/opt/recsys/apps/data-platform/data-generator/src:/opt/recsys "
            "python apps/data-platform/data-generator/src/scripts/generate_historical_to_minio.py "
            "--config $DATA_GENERATOR_CONFIG "
            "--target s3 --bucket $LAKE_BUCKET --prefix raw",
            mesh=False,
        )
        ingest_historical_batch_to_lakehouse = pod_task(
            "ingest_historical_batch_to_lakehouse",
            DATAFLOW_IMAGE,
            "python -m ingest.batch_lakehouse_ingestion "
            "--run-path s3a://$LAKE_BUCKET/raw/$DATA_GENERATOR_RUN_ID "
            "--lakehouse-warehouse $LAKEHOUSE_WAREHOUSE "
            "--mode overwrite",
            mesh=False,
        )
        load_realtime_to_source_postgres = pod_task(
            "load_realtime_to_source_postgres",
            DATAFLOW_IMAGE,
            optional_command(
                "REALTIME_E2E_ENABLED",
                "PYTHONPATH=/opt/recsys/apps/data-platform/data-generator/src:/opt/recsys "
                "python apps/data-platform/data-generator/src/scripts/load_realtime_to_postgres.py "
                "--config $DATA_GENERATOR_CONFIG "
                "--limit-per-table $REALTIME_LIMIT_PER_TABLE",
                "realtime source load",
            ),
        )
        run_spark_batch_to_offline_store = pod_task(
            "run_spark_batch_to_offline_store",
            SPARK_IMAGE,
            spark_native_submit(
                "run_spark_batch_to_offline_store",
                "local:///opt/recsys/apps/data-platform/src/features/spark/spark_batch_entrypoint.py",
                "--config $SPARK_BATCH_CONFIG",
            ),
            mesh=False,
        )
        feast_materialize_incremental = pod_task(
            "feast_materialize_incremental",
            DATAFLOW_IMAGE,
            "cd /opt/recsys/apps/data-platform/feature-store/feature_repo && "
            "export FEAST_POSTGRES_HOST=${FEAST_POSTGRES_HOST:-feature-postgres} && "
            "export FEAST_POSTGRES_PORT=${FEAST_POSTGRES_PORT:-5432} && "
            "export FEAST_POSTGRES_DB=${FEAST_POSTGRES_DB:-feature_store} && "
            "export FEAST_POSTGRES_SCHEMA=${FEAST_POSTGRES_SCHEMA:-feature_store} && "
            "export FEAST_POSTGRES_USER=${FEAST_POSTGRES_USER:-feast} && "
            "export FEAST_POSTGRES_PASSWORD=${FEAST_POSTGRES_PASSWORD:-feast} && "
            "export FEAST_POSTGRES_SSLMODE=${FEAST_POSTGRES_SSLMODE:-disable} && "
            "python -c 'from feature_store.feast_registry import apply_feature_repo; apply_feature_repo(\".\")' && "
            "feast materialize-incremental $(date -u +%Y-%m-%dT%H:%M:%S)",
        )
        run_flink_stream_to_feature_stores = pod_task(
            "run_flink_stream_to_feature_stores",
            DATAFLOW_IMAGE,
            optional_command(
                "REALTIME_E2E_ENABLED",
                "python -c \"import json, urllib.request; "
                "jobs=json.load(urllib.request.urlopen('http://flink-jobmanager:8081/jobs/overview', timeout=10)); "
                "running=[job for job in jobs.get('jobs', []) if job.get('state') == 'RUNNING']; "
                "assert running, f'No RUNNING Flink jobs found: {jobs}'; "
                "print(json.dumps({'flink_running_jobs': len(running), 'job_ids': [job.get('jid') for job in running]}, sort_keys=True))\"",
                "realtime Flink feature-store sync",
            ),
        )
        offline_feature_drift = pod_task(
            "offline_feature_drift",
            DATAFLOW_IMAGE,
            "python -m validate.offline_feature_drift "
            "--report-path $OFFLINE_FEATURE_DRIFT_REPORT_PATH "
            "--current-feature-root $OFFLINE_FEATURE_DRIFT_CURRENT_ROOT "
            "--baseline-path $OFFLINE_FEATURE_DRIFT_BASELINE_PATH "
            "--sample-rows $OFFLINE_FEATURE_DRIFT_SAMPLE_ROWS "
            "--current-days $OFFLINE_FEATURE_DRIFT_CURRENT_DAYS "
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
            "--pipeline-arg source_run_path=s3a://$LAKE_BUCKET/raw/$DATA_GENERATOR_RUN_ID",
        )
        datahub_ingest = pod_task(
            "datahub_ingest",
            DATAFLOW_IMAGE,
            optional_command(
                "DATAHUB_INGEST_ENABLED",
                "python -m metadata.ingest_datahub_governance "
                "--gms-url $DATAHUB_GMS_URL "
                "--pushgateway-url $PUSHGATEWAY_URL",
                "DataHub governance ingest",
            ),
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
        run_spark_batch_to_offline_store >> feast_materialize_incremental >> offline_feature_drift >> trigger_kubeflow_retrain
        [trigger_kubeflow_retrain, run_flink_stream_to_feature_stores] >> datahub_ingest >> end
