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
    assert "great_expectations" not in dataflow_cli
    assert "dbt-core" not in dataflow_cli
    assert " pandas" not in spark_dockerfile


def test_airflow_dags_run_native_lakehouse_tasks_only():
    full_dag = (ROOT / "apps/data-platform/src/orchestration/airflow/dags/full_dataflow_local_dag.py").read_text()
    k8s_dag = (ROOT / "apps/data-platform/src/orchestration/airflow/dags/k8s_data_platform_dag.py").read_text()
    for name, source in [("full", full_dag), ("k8s", k8s_dag)]:
        assert "register_debezium_connector" in source
        assert "ingest_historical_batch_to_lakehouse" in source
        assert "batch_lakehouse_ingestion.py" in source
        assert "spark_batch_entrypoint.py" in source
        assert "realtime_stream_job.py" in source
        assert "--offline-store-enabled" in source
        assert "flink run -m flink-jobmanager:8081" in source
        if name == "k8s":
            assert "offline_feature_drift" in source
            assert "trigger_kubeflow_retrain" in source
            assert "offline_feature_drift >> trigger_kubeflow_retrain" in source
        assert "validate_" not in source
        assert "materialize_offline_to_online" not in source


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
    assert "LAKEHOUSE_WAREHOUSE" in rendered
    assert "ICEBERG_CATALOG" in rendered
    assert "ICEBERG_LAKEHOUSE_NAMESPACE" in rendered
    assert "OFFLINE_FEATURE_STORE_WAREHOUSE" in rendered
    assert "OFFLINE_FEATURE_CATALOG" in rendered
    assert "OFFLINE_STORE_ENABLED" in rendered
    assert "OFFLINE_FEATURE_DRIFT_REPORT_PATH" in rendered
    assert "RETRAIN_PSI_THRESHOLD" in rendered
    assert "register-realtime-cdc-connector" in rendered
    assert "--offline-store-enabled" in rendered


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
        "recsys-dataflow-allow",
        "recsys-kubeflow-allow",
        "recsys-mlflow-allow",
        "recsys-kserve-allow",
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


def test_spark_batch_config_uses_iceberg_tables():
    config = yaml.safe_load((ROOT / "configs/local/spark_batch.yaml").read_text())
    assert config["input"]["source"] == "lakehouse"
    output = config["output"]
    assert output["lakehouse_warehouse"] == "s3a://recsys-lakehouse/warehouse"
    assert output["iceberg_catalog"] == "recsys"
    assert output["iceberg_lakehouse_namespace"] == "lakehouse"
    assert output["offline_feature_catalog"] == "recsys_features"
    assert output["offline_feature_store_warehouse"] == "s3a://recsys-offline-feature-store/warehouse"
    assert output["iceberg_feature_namespace"] == "feature_store"
    assert output["offline_feature_store_uri"] == "s3a://recsys-offline-feature-store/warehouse/feature_store"


def test_deleted_legacy_artifacts_are_absent():
    for relative in [
        "infra/docker/debezium/kafka-connect-s3-sink.json",
        "infra/docker/scripts/register_minio_sink_connector.sh",
        "infra/docker/scripts/validate_bronze_cdc.py",
        "apps/data-platform/great_expectations",
        "apps/data-platform/dbt",
        "apps/data-platform/feature-store",
        "apps/data-platform/src/feature_engineering/spark/spark_realtime_bronze_entrypoint.py",
    ]:
        assert not (ROOT / relative).exists()
