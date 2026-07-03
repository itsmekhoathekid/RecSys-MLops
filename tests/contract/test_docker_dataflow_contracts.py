from __future__ import annotations

import json
from pathlib import Path

import yaml

from config.storage_paths import lakehouse_warehouse_uri, offline_feature_uri, raw_uri


ROOT = Path(__file__).resolve().parents[2]
RUNTIME_ROOTS = [
    ROOT / "apps/data-platform",
    ROOT / "infra/helm/recsys-data-platform",
    ROOT / "infra/docker",
    ROOT / "configs/local",
]
LEGACY_TOKENS = [
    "great_expectations",
    "dbt",
    "kafka-minio",
    "s3-sink",
    "bronze/kafka",
    "validate_bronze",
    "spark_realtime_bronze",
    "local_poc",
]


def _runtime_text() -> str:
    chunks: list[str] = []
    for root in RUNTIME_ROOTS:
        if root.is_file():
            chunks.append(root.read_text(encoding="utf-8"))
            continue
        for path in root.rglob("*"):
            if path.is_file() and path.suffix in {".py", ".yaml", ".yml", ".json", ".md", ".sh", ".Dockerfile"}:
                chunks.append(path.read_text(encoding="utf-8"))
    return "\n".join(chunks)


def test_lakehouse_path_builders_point_to_iceberg_feature_store():
    assert raw_uri("run1", "behavior_events") == "s3a://recsys-lakehouse/raw/run1/behavior_events"
    assert lakehouse_warehouse_uri() == "s3a://recsys-lakehouse/warehouse"
    assert offline_feature_uri("item_features") == "s3a://recsys-offline-feature-store/warehouse/feature_store/item_features"


def test_no_legacy_runtime_tokens_remain():
    text = _runtime_text()
    for token in LEGACY_TOKENS:
        assert token not in text


def test_debezium_is_the_only_kafka_connect_runtime_connector():
    registrar = (ROOT / "apps/data-platform/src/ingest/register_k8s_connectors.py").read_text()
    connector = json.loads((ROOT / "infra/docker/debezium/postgres-connector.json").read_text())
    kafka_connect_dockerfile = (ROOT / "infra/docker/Dockerfile.kafka-connect").read_text()
    assert '"debezium": ("recsys-postgres-cdc", debezium_config)' in registrar
    assert "recsys-kafka-minio-raw-sink" not in registrar
    assert connector["config"]["connector.class"] == "io.debezium.connector.postgresql.PostgresConnector"
    assert "debezium/debezium-connector-postgresql" in kafka_connect_dockerfile
    assert "kafka-connect-s3" not in kafka_connect_dockerfile


def test_spark_and_flink_images_include_iceberg_without_pandas_runtime():
    spark_dockerfile = (ROOT / "apps/data-platform/Dockerfile.spark").read_text()
    flink_dockerfile = (ROOT / "apps/data-platform/Dockerfile.flink").read_text()
    dataflow_cli = (ROOT / "apps/data-platform/Dockerfile.dataflow-cli").read_text()
    assert "iceberg-spark-runtime-3.5_2.12" in spark_dockerfile
    assert "hudi-spark3.5-bundle_2.12" in spark_dockerfile
    assert "iceberg-flink-runtime-1.19" in flink_dockerfile
    assert "flink-sql-connector-kafka" in flink_dockerfile
    assert "psycopg[binary]" in flink_dockerfile
    assert "google-cloud-bigquery" not in flink_dockerfile
    assert "great_expectations" not in dataflow_cli
    assert "dbt-core" not in dataflow_cli
    assert " pandas" not in spark_dockerfile


def test_airflow_dags_run_native_lakehouse_tasks_only():
    full_dag = (ROOT / "apps/data-platform/src/orchestration/airflow/dags/full_dataflow_local_dag.py").read_text()
    k8s_dag = (ROOT / "apps/data-platform/src/orchestration/airflow/dags/k8s_data_platform_dag.py").read_text()
    for name, source in [("full", full_dag), ("k8s", k8s_dag)]:
        assert "register_debezium_connector" in source
        assert "ingest_historical_batch_to_lakehouse" in source
        assert "python -m ingest.batch_lakehouse_ingestion" in source
        assert "spark_batch_entrypoint.py" in source
        assert "feast_materialize_incremental" in source
        assert "feast materialize-incremental" in source
        if name == "k8s":
            assert "offline_feature_drift" in source
            assert "trigger_kubeflow_retrain" in source
            assert "feast_materialize_incremental >> offline_feature_drift >> trigger_kubeflow_retrain" in source
            assert "python -m validate.offline_feature_drift" in source
            assert '"offline_feature_drift",\n            DATAFLOW_IMAGE,' in source
            assert "apply_feature_repo" in source
            assert "http://flink-jobmanager:8081/jobs/overview" in source
            assert "No RUNNING Flink jobs found" in source
        else:
            assert "realtime_stream_job.py" in source
            assert "--offline-store-enabled" in source
            assert "flink run -m flink-jobmanager:8081" in source
            assert "feature_repo" in source
        assert "validate_" not in source


def test_k8s_airflow_spark_tasks_use_native_kubernetes_mode():
    source = (ROOT / "apps/data-platform/src/orchestration/airflow/dags/k8s_data_platform_dag.py").read_text()
    for expected in [
        "--master ${SPARK_K8S_MASTER:-k8s://https://kubernetes.default.svc}",
        "--deploy-mode cluster",
        "spark.kubernetes.container.image=${SPARK_K8S_IMAGE:-recsys-spark:local}",
        "spark.kubernetes.authenticate.driver.serviceAccountName",
        "spark.driver.memoryOverhead=${SPARK_K8S_DRIVER_MEMORY_OVERHEAD:-384m}",
        "spark.executor.instances=${SPARK_K8S_EXECUTOR_INSTANCES:-1}",
        "spark.executor.memoryOverhead=${SPARK_K8S_EXECUTOR_MEMORY_OVERHEAD:-384m}",
        "spark.kubernetes.submission.waitAppCompletion=true",
        "spark.kubernetes.submission.connectionTimeout=${SPARK_K8S_CONNECTION_TIMEOUT:-60000}",
        "spark.kubernetes.submission.requestTimeout=${SPARK_K8S_REQUEST_TIMEOUT:-180000}",
        "local:///opt/recsys/apps/data-platform/src/features/spark/spark_batch_entrypoint.py",
        "optional_command(",
        "REALTIME_E2E_ENABLED",
        "DATAHUB_INGEST_ENABLED",
    ]:
        assert expected in source
    assert "local:///opt/recsys/apps/data-platform/src/validate/offline_feature_drift.py" not in source


def test_lakehouse_batch_ingestion_uses_python_not_spark_submit():
    k8s_dag = (ROOT / "apps/data-platform/src/orchestration/airflow/dags/k8s_data_platform_dag.py").read_text()
    local_dag = (ROOT / "apps/data-platform/src/orchestration/airflow/dags/full_dataflow_local_dag.py").read_text()
    ingestion_source = (ROOT / "apps/data-platform/src/ingest/batch_lakehouse_ingestion.py").read_text()
    assert "python -m ingest.batch_lakehouse_ingestion" in k8s_dag
    assert "python -m ingest.batch_lakehouse_ingestion" in local_dag
    assert '"ingest_historical_batch_to_lakehouse",\n            DATAFLOW_IMAGE,' in k8s_dag
    assert "local:///opt/recsys/apps/data-platform/src/ingest/batch_lakehouse_ingestion.py" not in k8s_dag
    assert "/opt/spark/bin/spark-submit " not in ingestion_source
    assert "spark_session(" not in ingestion_source
    assert "pyarrow.parquet" in ingestion_source


def test_k8s_airflow_task_pods_can_skip_istio_mesh_for_native_jobs():
    source = (ROOT / "apps/data-platform/src/orchestration/airflow/dags/k8s_data_platform_dag.py").read_text()
    assert '"sidecar.istio.io/inject": "false"' in source
    assert "mesh=False" in source
    assert "curl --max-time 5 -sf -X POST http://127.0.0.1:15020/quitquitquit" not in source
    assert "startup_timeout_seconds=600" in source


def test_airflow_runtime_disables_bytecode_writes_for_non_root_user():
    dockerfile = (ROOT / "infra/docker/Dockerfile.airflow").read_text()
    chart = (ROOT / "infra/helm/recsys-data-platform/templates/airflow.yaml").read_text()
    assert "ENV PYTHONDONTWRITEBYTECODE=1" in dockerfile
    assert "PYTHONDONTWRITEBYTECODE" in chart
    assert "timeout 120 airflow db check-migrations --migration-wait-timeout 120 || true" in chart
    assert "airflow db migrate &&" not in chart
    assert 'value: "900"' in chart


def test_flink_runtime_uses_fixed_mesh_friendly_internal_ports():
    chart = ROOT / "infra/helm/recsys-data-platform"
    security_chart = ROOT / "infra/helm/recsys-security"
    rendered = "\n".join(path.read_text() for path in (chart / "templates").glob("*.yaml"))
    security_rendered = "\n".join(path.read_text() for path in (security_chart / "templates").glob("*.yaml"))
    for expected in [
        "jobmanager.rpc.port: 6123",
        "blob.server.port: 6124",
        "taskmanager.data.port: 6121",
        "taskmanager.rpc.port: 6122",
        "query.server.port: 6125",
        "pekko.remote.startup-timeout: 60 s",
        "containerPort: 6121",
        "containerPort: 6122",
        "containerPort: 6125",
        "type: Recreate",
    ]:
        assert expected in rendered
    assert '"6121", "6122", "6123", "6124", "6125"' in security_rendered


def test_helm_exposes_iceberg_lakehouse_runtime_config():
    chart = ROOT / "infra/helm/recsys-data-platform"
    values = yaml.safe_load((chart / "values.yaml").read_text())
    rendered = (chart / "values.yaml").read_text() + "\n".join(
        path.read_text() for path in (chart / "templates").glob("*.yaml")
    )
    assert values["lakehouse"]["catalog"] == "recsys"
    assert values["lakehouse"]["lakehouseNamespace"] == "lakehouse"
    assert values["lakehouse"]["offlineFeatureCatalog"] == "recsys_features"
    assert values["lakehouse"]["featureNamespace"] == "feature_store"
    assert values["realtimeCdcConnector"]["enabled"] is True
    assert values["e2e"]["realtimeEnabled"] == "true"
    assert values["e2e"]["datahubIngestEnabled"] == "false"
    assert values["realtimeFlinkConsumer"]["offlineStoreSink"] == "postgres"
    assert values["featurePostgres"]["name"] == "feature-postgres"
    assert values["featurePostgres"]["schema"] == "feature_store"
    assert "LAKEHOUSE_WAREHOUSE" in rendered
    assert "ICEBERG_CATALOG" in rendered
    assert "ICEBERG_LAKEHOUSE_NAMESPACE" in rendered
    assert "OFFLINE_FEATURE_STORE_WAREHOUSE" in rendered
    assert "OFFLINE_FEATURE_CATALOG" in rendered
    assert "OFFLINE_STORE_ENABLED" in rendered
    assert "OFFLINE_STORE_SINK" in rendered
    assert '--offline-store-sink "$OFFLINE_STORE_SINK"' in rendered
    assert '--feast-postgres-host "$FEAST_POSTGRES_HOST"' in rendered
    assert '--feast-postgres-database "$FEAST_POSTGRES_DB"' in rendered
    assert '--feast-postgres-password "$FEAST_POSTGRES_PASSWORD"' in rendered
    assert "OFFLINE_FEATURE_DRIFT_REPORT_PATH" in rendered
    assert "OFFLINE_FEATURE_DRIFT_CURRENT_ROOT" in rendered
    assert "OFFLINE_FEATURE_DRIFT_BASELINE_PATH" in rendered
    assert "OFFLINE_FEATURE_DRIFT_SAMPLE_ROWS" in rendered
    assert "OFFLINE_FEATURE_DRIFT_TABLES" in rendered
    assert "DATA_PLATFORM_DAG_SCHEDULE" in rendered
    assert "RETRAIN_PSI_THRESHOLD" in rendered
    assert "register-realtime-cdc-connector" in rendered
    assert "--offline-store-enabled" in rendered
    assert "SPARK_K8S_MASTER" in rendered
    assert "SPARK_K8S_EXECUTOR_INSTANCES" in rendered
    assert "SPARK_K8S_DRIVER_MEMORY_OVERHEAD" in rendered
    assert "SPARK_K8S_EXECUTOR_MEMORY_OVERHEAD" in rendered
    assert "DATA_GENERATOR_CONFIG" in rendered
    assert "SPARK_BATCH_CONFIG" in rendered
    assert "REALTIME_E2E_ENABLED" in rendered
    assert "DATAHUB_INGEST_ENABLED" in rendered
    assert "AWS_ACCESS_KEY_ID" in rendered
    assert "AWS_SECRET_ACCESS_KEY" in rendered
    assert "deletecollection" in rendered
    assert "AIRFLOW__DAG_PROCESSOR__DAG_FILE_PROCESSOR_TIMEOUT" in rendered
    assert "{{- if .Values.realtimeCdcConnector.enabled }}" in rendered


def test_e2e_1k_whole_run_data_setup_configs_are_wired_into_helm_values():
    chart = ROOT / "infra/helm/recsys-data-platform"
    values = yaml.safe_load((chart / "values.yaml").read_text())
    generator = yaml.safe_load((ROOT / values["dataSetup"]["generatorConfig"]).read_text())
    spark_batch = yaml.safe_load((ROOT / values["dataSetup"]["sparkBatchConfig"]).read_text())
    assert generator["traffic"]["target_behavior_events"] == 1000
    assert generator["output"]["run_id"] == values["dataSetup"]["generatorRunId"] == "test_1k_seed42"
    assert spark_batch["input"]["source"] == "parquet"
    assert spark_batch["input"]["run_path"] == "s3a://recsys-lakehouse/warehouse/lakehouse"
    assert spark_batch["processing"]["mode"] == "whole_run"
    assert values["spark"]["executorInstances"] == "1"
    assert values["spark"]["driverMemoryOverhead"] == "128m"
    assert values["spark"]["executorMemoryOverhead"] == "128m"


def test_security_chart_declares_vault_external_secrets_and_istio_policies():
    chart = ROOT / "infra/helm/recsys-security"
    rendered = (chart / "values.yaml").read_text() + "\n".join(
        path.read_text() for path in (chart / "templates").glob("*.yaml")
    )
    for expected in [
        "ClusterSecretStore",
        "external-secrets.io/v1",
        "recsys-data-platform-secret",
        "recsys-mlflow-secrets",
        "recsys-mlops-runtime",
        "recsys-kserve-minio",
        "PeerAuthentication",
        "mode: STRICT",
        "AuthorizationPolicy",
        "recsys-kubeflow-allow",
        "recsys-kubeflow-ml-pipeline-api-allow",
        "recsys-kubeflow-ml-pipeline-permissive",
        "recsys-kubeflow-metadata-grpc-allow",
        "recsys-kubeflow-metadata-grpc-permissive",
        "recsys-kubeflow-seaweedfs-allow",
        "recsys-kubeflow-seaweedfs-permissive",
        "recsys-mlflow-allow",
        "recsys-kserve-allow",
        "namespaces:",
        "- kubeflow",
        "mode: PERMISSIVE",
        '"3306"',
        '"8080"',
        '"8887"',
        '"9000"',
        '"2181"',
        '"29092"',
        "cluster.local/ns/api-serving/sa/default",
        "cluster.local/ns/kubeflow/sa/pipeline-runner",
    ]:
        assert expected in rendered


def test_app_charts_do_not_render_literal_runtime_secrets_by_default():
    chart_roots = [
        ROOT / "infra/helm/recsys-data-platform",
        ROOT / "infra/helm/mlflow-stack",
        ROOT / "infra/helm/recsys-runtime",
        ROOT / "infra/helm/recsys-serving",
    ]
    rendered = "\n".join(
        (root / "values.yaml").read_text() + "\n".join(path.read_text() for path in (root / "templates").glob("*.yaml"))
        for root in chart_roots
    )
    for forbidden in [
        "rootPassword: minio123",
        "password: mlflow123",
        "secretAccessKey: minio123",
    ]:
        assert forbidden not in rendered
    for expected in [
        "secret:",
        "create: false",
        "recsys-data-platform-secret",
        "recsys-mlflow-secrets",
        "recsys-mlops-runtime",
        "recsys-kserve-minio",
    ]:
        assert expected in rendered


def test_spark_batch_config_reads_python_ingested_parquet_lakehouse():
    config = yaml.safe_load((ROOT / "configs/local/spark_batch.yaml").read_text())
    assert config["input"]["source"] == "parquet"
    assert config["input"]["run_path"] == "s3a://recsys-lakehouse/warehouse/lakehouse"
    assert config["processing"]["mode"] == "whole_run"
    output = config["output"]
    assert output["lakehouse_warehouse"] == "s3a://recsys-lakehouse/warehouse"
    assert output["iceberg_catalog"] == "recsys"
    assert output["iceberg_lakehouse_namespace"] == "lakehouse"
    assert output["offline_feature_catalog"] == "recsys_features"
    assert output["offline_feature_store_warehouse"] == "s3a://recsys-offline-feature-store/warehouse"
    assert output["iceberg_feature_namespace"] == "feature_store"
    assert output["offline_feature_store_uri"] == "s3a://recsys-offline-feature-store/warehouse/feature_store"


def test_spark_batch_entrypoint_processes_the_whole_run_in_one_commit():
    source = (ROOT / "apps/data-platform/src/features/spark/spark_batch_entrypoint.py").read_text()
    assert "batch_chunk_count" not in source
    assert "batch_chunk_commits" not in source
    assert "batch_commit_id" not in source
    assert 'write_iceberg_table(frame, table_name, mode="overwrite")' in source


def test_deleted_legacy_artifacts_are_absent():
    for relative in [
        "infra/docker/debezium/kafka-connect-s3-sink.json",
        "infra/docker/scripts/register_minio_sink_connector.sh",
            "infra/docker/scripts/validate_bronze_cdc.py",
            "apps/data-platform/great_expectations",
            "apps/data-platform/dbt",
            "apps/data-platform/src/features/spark/spark_realtime_bronze_entrypoint.py",
        ]:
            assert not (ROOT / relative).exists()
    assert (ROOT / "apps/data-platform/feature-store/feature_repo/feature_store.yaml").exists()
