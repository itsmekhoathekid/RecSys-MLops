from __future__ import annotations

import json
import os
from pathlib import Path

import yaml

from data_generator.scripts.run_realtime_postgres_producer import build_event_rows
from pipelines.data_pipeline.feature_engineering.flink.realtime_stream_job import normalize_event
from pipelines.data_pipeline.config.storage_paths import (
    bronze_kafka_uri,
    ml_artifact_uri,
    offline_feature_uri,
    raw_uri,
    silver_uri,
)
from data_generator.sinks.postgres_sink import build_upsert_sql, normalize_postgres_row
from pipelines.data_pipeline.ingest.bronze_cdc_reader import normalize_behavior_events_from_cdc


ROOT = Path(__file__).resolve().parents[2]


def test_two_bucket_path_builders_keep_logical_boundary():
    assert raw_uri("run1", "behavior_events") == "s3a://recsys-lake/raw/run1/behavior_events"
    assert bronze_kafka_uri("cdc.behavior_events") == "s3a://recsys-lake/bronze/kafka/cdc.behavior_events"
    assert silver_uri("clean_behavior_events") == "s3a://recsys-lake/silver/clean_behavior_events"
    assert ml_artifact_uri("ml_bst_training") == "s3a://recsys-lake/silver/ml/ml_bst_training"
    assert offline_feature_uri("item_features") == "s3a://recsys-feature-store/offline/item_features"


def test_config_uses_two_minio_buckets():
    config = yaml.safe_load((ROOT / "config/data_flow.yaml").read_text())
    assert config["lake"]["lake_bucket"] == "recsys-lake"
    assert config["lake"]["feature_store_bucket"] == "recsys-feature-store"
    assert config["feature_store_offline"]["bucket"] == "recsys-feature-store"
    assert config["ml_artifacts"]["bucket"] == "recsys-lake"


def test_feast_config_points_to_feature_store_bucket_only():
    config = yaml.safe_load((ROOT / "config/feast.yaml").read_text())
    paths = config["feature_paths"].values()
    assert all(path.startswith("s3://recsys-feature-store/offline/") for path in paths)
    assert not any("recsys-lake" in path for path in paths)


def test_kafka_s3_sink_targets_lake_bronze_only():
    connector = json.loads(
        (ROOT / "deployments/docker/debezium/kafka-connect-s3-sink.json").read_text()
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
        (ROOT / "deployments/docker/docker-compose.dataflow.yml").read_text()
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
    assert services["dataflow-cli"]["build"]["dockerfile"] == "deployments/docker/Dockerfile.dataflow-cli"
    assert services["kafka-connect"]["build"]["dockerfile"] == "deployments/docker/Dockerfile.kafka-connect"


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
        ROOT / "pipelines/data_pipeline/orchestration/airflow/dags/full_dataflow_local_dag.py"
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
    assert "python3 -m pipelines.data_pipeline.feature_engineering.flink.realtime_stream_job" in dag_source


def test_makefile_exposes_pipeline_operation_targets():
    makefile = (ROOT / "Makefile").read_text()
    for target in [
        "dataflow-e2e",
        "dataflow-ingest-lake",
        "dataflow-realtime-up",
        "dataflow-realtime-down",
    ]:
        assert f".PHONY: {target}" in makefile
        assert f"{target}:" in makefile


def test_dataflow_operation_scripts_are_executable_and_use_expected_entrypoints():
    scripts = {
        "dataflow_run_e2e.sh": "dataflow_trigger_dag.sh",
        "dataflow_ingest_lake.sh": "generate_historical_to_minio",
        "dataflow_realtime_up.sh": "run_realtime_postgres_producer.py",
        "dataflow_realtime_down.sh": "recsys-dataflow-realtime-producer",
    }
    for filename, expected in scripts.items():
        path = ROOT / "deployments/docker/scripts" / filename
        assert os.access(path, os.X_OK), f"{filename} should be executable"
        assert expected in path.read_text()


def test_connector_smoke_checks_task_state():
    smoke_source = (ROOT / "deployments/docker/scripts/smoke_check_stack.py").read_text()
    assert "connector task not RUNNING" in smoke_source
    assert 'task.get("state") != "RUNNING"' in smoke_source


def test_e2e_trigger_unpauses_dag_before_triggering():
    trigger_source = (ROOT / "deployments/docker/scripts/dataflow_trigger_dag.sh").read_text()
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
