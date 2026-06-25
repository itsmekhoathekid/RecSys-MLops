from __future__ import annotations

import json
import os
from pathlib import Path

import yaml

from scripts.run_realtime_postgres_producer import build_event_rows
from feature_engineering.flink.realtime_stream_job import normalize_event
from config.storage_paths import (
    bronze_kafka_uri,
    ml_artifact_uri,
    offline_feature_uri,
    raw_uri,
    silver_uri,
)
from sinks.postgres_sink import build_upsert_sql, normalize_postgres_row
from ingest.bronze_cdc_reader import normalize_behavior_events_from_cdc


ROOT = Path(__file__).resolve().parents[2]


def test_two_bucket_path_builders_keep_logical_boundary():
    assert raw_uri("run1", "behavior_events") == "s3a://recsys-lake/raw/run1/behavior_events"
    assert bronze_kafka_uri("cdc.behavior_events") == "s3a://recsys-lake/bronze/kafka/cdc.behavior_events"
    assert silver_uri("clean_behavior_events") == "s3a://recsys-lake/silver/clean_behavior_events"
    assert ml_artifact_uri("ml_bst_training") == "s3a://recsys-lake/silver/ml/ml_bst_training"
    assert offline_feature_uri("item_features") == "s3a://recsys-feature-store/offline/item_features"


def test_config_uses_two_minio_buckets():
    config = yaml.safe_load((ROOT / "configs/local/data_flow.yaml").read_text())
    assert config["lake"]["lake_bucket"] == "recsys-lake"
    assert config["lake"]["feature_store_bucket"] == "recsys-feature-store"
    assert config["feature_store_offline"]["bucket"] == "recsys-feature-store"
    assert config["ml_artifacts"]["bucket"] == "recsys-lake"


def test_feast_config_points_to_feature_store_bucket_only():
    config = yaml.safe_load((ROOT / "configs/local/feast.yaml").read_text())
    paths = config["feature_paths"].values()
    assert all(path.startswith("s3://recsys-feature-store/offline/") for path in paths)
    assert not any("recsys-lake" in path for path in paths)


def test_kafka_s3_sink_targets_lake_bronze_only():
    connector = json.loads(
        (ROOT / "infra/docker/debezium/kafka-connect-s3-sink.json").read_text()
    )
    cfg = connector["config"]
    assert cfg["s3.bucket.name"] == "recsys-lake"
    assert cfg["topics.dir"] == "bronze/kafka"
    assert cfg["path.format"] == "'event_date='YYYY-MM-dd"
    assert cfg["flush.size"] == "1"
    assert int(cfg["partition.duration.ms"]) > 0
    assert "recsys-feature-store" not in json.dumps(cfg)


def test_compose_declares_expected_services_and_images():
    compose = yaml.safe_load(
        (ROOT / "infra/docker/docker-compose.dataflow.yml").read_text()
    )
    services = compose["services"]
    for name in [
        "base-python",
        "postgres",
        "minio",
        "minio-init",
        "zookeeper",
        "kafka",
        "schema-registry",
        "kafka-connect",
        "redis",
        "spark-master",
        "spark-worker",
        "flink-jobmanager",
        "flink-taskmanager",
        "airflow-postgres",
        "airflow-init",
        "airflow-webserver",
        "airflow-scheduler",
        "dataflow-cli",
    ]:
        assert name in services
    assert services["dataflow-cli"]["build"]["dockerfile"] == "apps/data-platform/Dockerfile.dataflow-cli"
    assert services["kafka-connect"]["build"]["dockerfile"] == "infra/docker/Dockerfile.kafka-connect"


def test_postgres_loader_uses_idempotent_upsert_contract():
    sql = build_upsert_sql("users", ["user_id", "email", "city"])
    assert "ON CONFLICT (user_id) DO UPDATE SET" in sql
    assert "email = EXCLUDED.email" in sql
    assert "city = EXCLUDED.city" in sql


def test_postgres_loader_normalizes_nullable_user_preference_pk():
    row = normalize_postgres_row("user_preferences", {"user_id": 1, "category_id": 2, "brand_id": None})
    assert row["brand_id"] == 0


def test_full_dataflow_dag_declares_historical_and_realtime_task_groups():
    dag_source = (
        ROOT / "apps/data-platform/src/orchestration/airflow/dags/full_dataflow_local_dag.py"
    ).read_text()
    for group_id in [
        "platform_init",
        "historical_bootstrap_path",
        "realtime_cdc_path",
        "realtime_batch_to_offline_path",
        "realtime_stream_to_online_path",
        "final_validation",
    ]:
        assert f'TaskGroup("{group_id}")' in dag_source
    assert "spark_realtime_bronze_entrypoint.py" in dag_source
    assert "realtime_stream_job" in dag_source
    assert 'command=["bash", "-lc", f"{command} "]' in dag_source
    assert "flink run -m flink-jobmanager:8081" in dag_source
    assert "-py apps/data-platform/src/feature_engineering/flink/realtime_stream_job.py" in dag_source


def test_single_streaming_dag_runs_real_stream_job_without_masking_failures():
    dag_source = (
        ROOT / "apps/data-platform/src/orchestration/airflow/dags/streaming_feature_pipeline_dag.py"
    ).read_text()

    assert "feature_engineering/flink/realtime_stream_job.py" in dag_source
    assert "--runner pyflink --help" in dag_source
    assert "--runner direct" not in dag_source
    assert "|| true" not in dag_source


def test_k8s_data_platform_dag_declares_required_order():
    dag_source = (
        ROOT / "apps/data-platform/src/orchestration/airflow/dags/k8s_data_platform_dag.py"
    ).read_text()
    for task_id in [
        "init_data_platform_minio",
        "init_source_schema",
        "init_warehouse",
        "register_debezium_connector",
        "register_kafka_minio_sink",
        "generate_historical_to_lake_raw",
        "load_realtime_to_source_postgres",
        "wait_for_cdc_to_bronze",
        "ingest_historical_to_staging",
        "run_flink_processing",
        "ge_validate_staging",
        "dbt_transform_production",
        "write_offline_feature_store",
        "validate_offline_feature_store",
        "sync_offline_to_online_store",
        "evidently_feature_drift",
        "datahub_ingest",
        "final_smoke",
    ]:
        assert task_id in dag_source
    assert "KubernetesPodOperator" in dag_source
    assert "--warehouse-enabled" in dag_source
    assert "materialize_offline_to_online.py" in dag_source
    assert "FEAST_REGISTRY_BACKUP_URI" in dag_source
    assert "ingest.register_k8s_connectors --connector debezium" in dag_source
    assert "generate_historical_to_minio.py" in dag_source
    assert "validate_bronze_cdc.py" in dag_source
    materialize_source = (ROOT / "apps/data-platform/feature-store/src/materialize_offline_to_online.py").read_text()
    feature_view_source = (ROOT / "apps/data-platform/feature-store/feature_repo/feature_views.py").read_text()
    assert "apply_and_materialize_incremental" in materialize_source
    assert "Array(Int64)" in feature_view_source
    assert "Array(String)" in feature_view_source


def test_dbt_project_uses_production_schema_without_target_prefix():
    macro = (
        ROOT / "apps/data-platform/dbt/recsys_warehouse/macros/generate_schema_name.sql"
    ).read_text()
    assert "{{ custom_schema_name | trim }}" in macro
    assert "target.schema ~" not in macro


def test_k8s_data_platform_helm_chart_declares_core_services():
    chart = ROOT / "infra/helm/recsys-data-platform"
    values = yaml.safe_load((chart / "values.yaml").read_text())
    assert values["warehousePostgres"]["name"] == "warehouse-postgres"
    assert values["images"]["airflow"] == "recsys-airflow:local"
    assert values["images"]["kafkaConnect"] == "recsys-kafka-connect:local"
    assert values["kafkaConnect"]["name"] == "kafka-connect"
    assert values["minio"]["name"] == "data-platform-minio"
    assert values["minio"]["endpoint"] == "http://data-platform-minio:9000"
    assert values["realtimeProducer"]["enabled"] is True
    assert values["realtimeProducer"]["name"] == "realtime-event-producer"
    assert values["realtimeFlinkConsumer"]["enabled"] is True
    assert values["realtimeFlinkConsumer"]["name"] == "realtime-flink-consumer"
    rendered_sources = (chart / "values.yaml").read_text() + "\n".join(
        path.read_text() for path in (chart / "templates").glob("*.yaml")
    )
    for expected in [
        "warehouse-postgres",
        "source-postgres",
        "airflow-postgres",
        "flink-jobmanager",
        "flink-taskmanager",
        "redis",
        "kafka",
        "kafka-connect",
        "data-platform-minio",
        "init-data-platform-minio",
        "init-warehouse",
        "init-source-schema",
        "helm.sh/hook-weight",
        "register-realtime-cdc-connector",
        "ingest.register_k8s_connectors",
        "realtime-event-producer",
        "set -euo pipefail",
        "init_postgres_schema.py",
        "run_realtime_postgres_producer.py",
        "realtime-flink-consumer",
        "flink run -m flink-jobmanager:8081",
        "realtime_stream_job.py",
        "--runner pyflink",
        "--continuous",
        "blob.server.port: 6124",
        "name: blob",
        "pyflink-udf-runner.sh",
        "py4j-0.10.9.7-src.zip",
    ]:
        assert expected in rendered_sources
    assert "REALTIME_MAX_EVENTS" in rendered_sources
    assert "REALTIME_STREAM_GROUP_ID" in rendered_sources
    assert "FEAST_REGISTRY_BACKUP_URI" in rendered_sources
    assert "PUSHGATEWAY_URL" in rendered_sources
    assert "trigger_kubeflow_retrain" in (ROOT / "apps/data-platform/src/orchestration/airflow/dags/k8s_data_platform_dag.py").read_text()
    assert "monitoring-sql-exporter" in rendered_sources
    assert "PYTHONPATH: /opt/flink/opt/python:" in rendered_sources
    dockerfile_flink = (ROOT / "apps/data-platform/Dockerfile.flink").read_text()
    assert "/opt/flink/opt/python/pyflink.zip" in dockerfile_flink
    assert "python3 -m zipfile -e /opt/flink/opt/python/pyflink.zip" in dockerfile_flink
    assert "/usr/local/bin/python" in dockerfile_flink
    assert "apache-beam==2.48.0" in dockerfile_flink
    assert "avro-python3==1.10.2" in dockerfile_flink
    assert "py4j==0.10.9.7" in dockerfile_flink
    init_source_schema = (ROOT / "infra/docker/scripts/init_postgres_schema.py").read_text()
    assert "pg_advisory_xact_lock" in init_source_schema
    assert "minio.experiment-tracking" not in rendered_sources


def test_mlflow_stack_keeps_separate_model_artifact_minio():
    values = yaml.safe_load((ROOT / "infra/helm/mlflow-stack/values.yaml").read_text())
    assert values["namespace"]["name"] == "experiment-tracking"
    assert values["minio"]["bucket"] == "mlflow-artifacts"
    assert values["minio"]["modelStoreBucket"] == "recsys-model-store"


def test_dbt_project_defines_staging_sources_and_production_models():
    dbt_root = ROOT / "apps/data-platform/dbt/recsys_warehouse"
    project = yaml.safe_load((dbt_root / "dbt_project.yml").read_text())
    assert project["profile"] == "recsys_warehouse"
    sources = (dbt_root / "models/sources.yml").read_text()
    assert "stream_behavior_events" in sources
    for model in [
        "fact_behavior_events.sql",
        "fact_impressions.sql",
        "fact_orders.sql",
        "dim_products_scd.sql",
    ]:
        assert (dbt_root / "models/production" / model).exists()


def test_makefile_exposes_pipeline_operation_targets():
    makefile = (ROOT / "Makefile").read_text()
    for target in [
        "cluster-up",
        "cluster-down",
        "cluster-destroy",
        "cluster-status",
        "cluster-data-setup",
        "cluster-mlops-serving-e2e",
        "dataflow-e2e",
        "dataflow-ingest-lake",
        "dataflow-realtime-up",
        "dataflow-realtime-down",
        "data-platform-e2e",
        "data-platform-run-status",
        "data-platform-verify-e2e",
        "data-platform-stream-generator-start",
        "data-platform-stream-generator-stop",
        "data-platform-stream-generator-status",
        "observability-template",
        "observability-install",
        "observability-port-forward",
        "observability-demo-traffic",
    ]:
        assert f".PHONY: {target}" in makefile
        assert f"{target}:" in makefile
    assert "recsys-kafka-connect:local" in makefile
    assert "DATA_PLATFORM_REALTIME_PRODUCER ?= realtime-event-producer" in makefile
    assert "cluster-up: mlops-cluster-up" in makefile
    assert "cluster-down: mlops-cluster-down" in makefile
    assert "cluster-destroy: mlops-cluster-destroy" in makefile
    assert "cluster-data-setup: mlops-cluster-data-setup" in makefile
    assert "cluster-mlops-serving-e2e: mlops-cluster-serving-e2e" in makefile
    assert "cluster-status: mlops-cluster-status" in makefile
    assert "--set observability.serviceMonitor.enabled=false" in makefile
    assert "--set autoscaling.kserveResource.enabled=false" in makefile
    assert "--set autoscaling.kserveResource.enabled=true" in makefile


def test_cluster_lifecycle_scripts_manage_full_service_stack():
    up_script = (ROOT / "infra/k8s/scripts/mlops_cluster_up.sh").read_text()
    down_script = (ROOT / "infra/k8s/scripts/mlops_cluster_down.sh").read_text()
    destroy_script = (ROOT / "infra/k8s/scripts/mlops_cluster_destroy.sh").read_text()
    data_setup_script = (ROOT / "infra/k8s/scripts/cluster_data_setup.sh").read_text()
    serving_e2e_script = (ROOT / "infra/k8s/scripts/cluster_mlops_serving_e2e.sh").read_text()
    readme = (ROOT / "README.md").read_text()

    for expected in [
        "install_kfp_if_needed",
        "install_kuberay_if_needed",
        "install_keda_if_needed",
        "install_cert_manager_if_needed",
        "install_kserve_if_needed",
        "certificates.cert-manager.io",
        "kubectl apply --server-side --force-conflicts",
        "scale_optional_kfp_components",
        "run_make observability-install",
        "run_make mlops-install-stack",
        "run_make data-platform-install",
        "run_make mlops-install-serving",
        "run_make gateway-install-controller",
        "run_make gateway-install",
        "Verify Required Deployments",
        "Verify Required Services",
        "recsys-api-serving",
        "recsys-grafana",
        "keda-add-ons-http-interceptor",
    ]:
        assert expected in up_script

    for expected in [
        "cluster-down is non-destructive",
        "kubectl get pvc -A",
        "Current Full Service Namespaces",
        "minikube -p \"${PROFILE}\" stop",
    ]:
        assert expected in down_script
    assert "delete_namespace" not in down_script
    assert "uninstall_release" not in down_script

    for expected in [
        "uninstall_release recsys-gateway api-serving",
        "uninstall_release recsys-serving kserve-triton-inference",
        "uninstall_release recsys-observability observability",
        "uninstall_release recsys-data-platform recsys-dataflow",
        "uninstall_release recsys-mlflow experiment-tracking",
        "delete_namespace",
        "delete_kserve_webhooks",
        "clear_namespaced_resource_finalizers kserve-triton-inference inferenceservices.serving.kserve.io",
        "Verify Services Removed",
        "minikube -p \"${PROFILE}\" stop",
    ]:
        assert expected in destroy_script

    for expected in [
        "airflow dags trigger \"${DAG_ID}\" --run-id \"${RUN_ID}\"",
        "wait_for_airflow_run",
        "data-platform-verify-e2e",
        "fs:user_sequence",
        "Redis Online Store",
    ]:
        source = data_setup_script + (ROOT / "apps/data-platform/src/local/verify_k8s_data_platform_e2e.py").read_text()
        assert expected in source

    for expected in [
        "submit_pipeline_run.py",
        "model_cd.py",
        "read_promotion_manifest",
        "http://127.0.0.1:${FASTAPI_PORT}/recommendations",
        "verify_grafana_dashboard",
        "sum(model_predictions_total)",
        "recsys_api_triton_inference_duration_seconds_count",
    ]:
        assert expected in serving_e2e_script

    for expected in [
        "make cluster-up",
        "make cluster-down",
        "make cluster-destroy",
        "make cluster-data-setup",
        "make cluster-mlops-serving-e2e",
        "RECSYS_CLUSTER_BUILD_IMAGES=1 make cluster-up",
        "RECSYS_CLUSTER_INSTALL_DATAHUB=1 make cluster-up",
        "RECSYS_CLUSTER_DELETE_PROFILE=1 make cluster-destroy",
    ]:
        assert expected in readme


def test_observability_helm_chart_declares_rubric_stack():
    chart = ROOT / "infra/helm/recsys-observability"
    values = yaml.safe_load((chart / "values.yaml").read_text())
    assert values["namespace"]["name"] == "observability"
    rendered_sources = (chart / "values.yaml").read_text() + "\n".join(
        path.read_text() for path in (chart / "templates").glob("*.yaml")
    ) + "\n".join(
        path.read_text() for path in (chart / "dashboards").glob("*.json")
    )
    for expected in [
        "recsys-prometheus",
        "recsys-grafana",
        "recsys-loki",
        "recsys-tempo",
        "recsys-promtail",
        "recsys-pushgateway",
        "redis-exporter",
        "warehouse-postgres-exporter",
        "Web API Overview",
        "Compute Telemetry",
        "Logs Overview",
        "Traces Overview",
        "ML Drift & Retrain",
        "recsys_api_requests_total",
        "recsys_ml_feature_drift_psi",
        "gridPos",
        "bargauge",
        "gauge",
        "traces",
        "Loki",
        "Tempo",
        "Model A/B Testing",
        "model_predictions_total",
        "model_prediction_latency_seconds",
        "model_prediction_confidence",
    ]:
        assert expected in rendered_sources
    dashboards = sorted((chart / "dashboards").glob("*.json"))
    assert len(dashboards) >= 8


def test_dataflow_operation_scripts_are_executable_and_use_expected_entrypoints():
    scripts = {
        "dataflow_run_e2e.sh": "dataflow_trigger_dag.sh",
        "dataflow_ingest_lake.sh": "generate_historical_to_minio",
        "dataflow_realtime_up.sh": "apps/data-platform/data-generator/src/scripts/run_realtime_postgres_producer.py",
        "dataflow_realtime_down.sh": "recsys-dataflow-realtime-producer",
    }
    for filename, expected in scripts.items():
        path = ROOT / "infra/docker/scripts" / filename
        assert os.access(path, os.X_OK), f"{filename} should be executable"
        assert expected in path.read_text()


def test_connector_smoke_checks_task_state():
    smoke_source = (ROOT / "infra/docker/scripts/smoke_check_stack.py").read_text()
    assert "connector task not RUNNING" in smoke_source
    assert 'task.get("state") != "RUNNING"' in smoke_source


def test_e2e_trigger_unpauses_dag_before_triggering():
    trigger_source = (ROOT / "infra/docker/scripts/dataflow_trigger_dag.sh").read_text()
    assert 'airflow dags unpause "${DAG_ID}"' in trigger_source
    assert 'airflow dags trigger "${DAG_ID}"' in trigger_source


def test_continuous_realtime_producer_rows_match_source_system_shape():
    from datetime import datetime, timezone

    rows = build_event_rows(2, datetime(2026, 1, 1, tzinfo=timezone.utc), 3, 5)
    assert rows["behavior_events"]["event_type"] == "purchase"
    assert rows["behavior_events"]["event_id"].startswith("continuous-event-")
    assert rows["behavior_events"]["order_id"] == rows["orders"]["order_id"]
    assert rows["order_items"]["product_id"] == rows["behavior_events"]["product_id"]


def test_realtime_stream_normalize_tolerates_debezium_decimal_bytes():
    event = normalize_event(
        {
            "event_id": "evt-1",
            "user_id": 1,
            "product_id": 2,
            "event_type": "view",
            "event_timestamp": "2026-01-01T00:00:00Z",
            "category_id": 3,
            "brand_id": 4,
            "price": "CDM=",
            "price_bucket": 5,
        }
    )

    assert event is not None
    assert event["price"] == 5.0
    assert event["price_bucket"] == 5


def test_bronze_cdc_normalize_falls_back_price_to_price_bucket_for_decimal_bytes():
    import pandas as pd

    events = normalize_behavior_events_from_cdc(
        pd.DataFrame(
            [
                {
                    "event_id": "evt-1",
                    "user_id": "1",
                    "product_id": "2",
                    "event_type": "view",
                    "event_timestamp": "2026-01-01T00:00:00Z",
                    "category_id": "3",
                    "brand_id": "4",
                    "price": "TTc=",
                    "price_bucket": "57",
                }
            ]
        )
    )

    assert events.loc[0, "price"] == 57.0
