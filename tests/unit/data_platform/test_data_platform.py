from __future__ import annotations

import json
import math
from pathlib import Path

from feature_engineering.flink.candidate_pool_job import candidate_updates
from feature_engineering.flink.item_features_job import ItemFeatureState
from feature_engineering.flink.realtime_stream_job import (
    StreamQualityTracker,
    build_offline_feature_rows,
    build_realtime_feature_payloads,
    normalize_event,
    parse_message,
)
from feature_engineering.flink.user_aggregate_job import UserAggregateState
from feature_engineering.flink.user_sequence_job import UserSequenceState
from feature_store.online_writer import dumps_feature_payload
from ingest.debezium import extract_debezium_after
from lakehouse.iceberg import IcebergCatalogConfig, create_flink_catalog_sql, spark_iceberg_conf
from mlops.trigger_kubeflow_retrain import default_pipeline_arguments, failed_features, parse_pipeline_args, trigger_retrain
from validate.offline_feature_drift import calculate_psi


def test_debezium_after_extraction_skips_deletes():
    assert extract_debezium_after({"payload": {"op": "d", "after": {"event_id": "e1"}}}) is None
    after = extract_debezium_after({"payload": {"op": "c", "after": {"event_id": "e2"}}})
    assert after == {"event_id": "e2"}
    assert parse_message(b'{"payload":{"op":"c","after":{"event_id":"e3"}}}') == {"event_id": "e3"}


def test_realtime_stream_event_normalization_defaults_optional_dimensions():
    event = normalize_event(
        {
            "event_id": "e1",
            "user_id": "1",
            "product_id": "10",
            "event_type": "view",
            "event_timestamp": "2026-01-01T00:00:00",
        }
    )
    assert event is not None
    assert event["user_id"] == 1
    assert event["event_type_id"] == 1
    assert event["category_id"] == 0


def test_streaming_payloads_candidate_updates_and_offline_rows():
    event = normalize_event(
        {
            "event_id": "e1",
            "user_id": "1",
            "product_id": "10",
            "event_type": "cart",
            "event_timestamp": "2026-01-01T00:00:00Z",
            "category_id": 2,
            "brand_id": 3,
            "price_bucket": 4,
            "price": 9.0,
        }
    )
    assert event is not None
    sequence, aggregate, item = build_realtime_feature_payloads(
        event,
        UserSequenceState(max_history_length=2),
        UserAggregateState(),
        ItemFeatureState(),
    )
    rows = build_offline_feature_rows(event, sequence, aggregate, item, "cdc.behavior_events", 60)
    assert rows["stream_behavior_events"][0]["event_id"] == "e1"
    assert rows["stream_user_sequence_features"][0]["sequence_length"] == 1
    assert rows["stream_user_aggregate_features"][0]["carts_30m"] == 1
    assert rows["stream_item_features"][0]["popularity_score"] == item["popularity_score"]
    assert ("candidate:trending:1h", 10, item["views_1h"] + item["carts_1h"] * 3.0) in candidate_updates(item)


def test_stream_quality_tracker_marks_bursty_and_late_windows():
    tracker = StreamQualityTracker("cdc.behavior_events", window_seconds=60, burst_threshold_event_count=2)
    assert tracker.update("2026-01-01T00:00:01Z", 10.0, False) == []
    assert tracker.update("2026-01-01T00:00:02Z", 120.0, True, is_duplicate=True) == []
    flushed = tracker.flush()
    assert len(flushed) == 1
    assert flushed[0].is_bursty is True
    assert flushed[0].late_event_count == 1
    assert flushed[0].duplicate_event_count == 1


def test_online_payload_serializer_replaces_nonfinite_values():
    payload = {"avg_viewed_price_7d": float("nan"), "history": [1, float("inf")]}
    rendered = dumps_feature_payload(payload)
    assert "NaN" not in rendered
    assert "Infinity" not in rendered
    assert '"avg_viewed_price_7d": null' in rendered


def test_iceberg_catalog_defaults_and_spark_conf():
    config = IcebergCatalogConfig()
    assert config.lakehouse_database == "recsys.lakehouse"
    assert config.lakehouse_table("behavior_events") == "recsys.lakehouse.behavior_events"
    assert config.feature_database == "recsys_features.feature_store"
    assert config.feature_table("item_features") == "recsys_features.feature_store.item_features"
    spark_conf = spark_iceberg_conf(config)
    assert spark_conf["spark.sql.catalog.recsys"] == "org.apache.iceberg.spark.SparkCatalog"
    assert spark_conf["spark.sql.catalog.recsys.warehouse"] == "s3a://recsys-lakehouse/warehouse"
    assert spark_conf["spark.sql.catalog.recsys_features.warehouse"] == "s3a://recsys-offline-feature-store/warehouse"
    assert "CREATE CATALOG recsys" in create_flink_catalog_sql(config)


def test_spark_feature_path_is_native_iceberg_not_pandas_or_parquet_writer():
    spark_dir = Path("apps/data-platform/src/feature_engineering/spark")
    sources = "\n".join(path.read_text(encoding="utf-8") for path in spark_dir.glob("*.py"))
    batch_source = (spark_dir / "spark_batch_entrypoint.py").read_text(encoding="utf-8")
    assert "import pandas" not in sources
    assert "pd." not in sources
    assert "from pyspark.sql" in sources
    assert 'source", os.getenv("SPARK_BATCH_SOURCE", "lakehouse")' in batch_source
    assert "write_iceberg_table" in batch_source
    assert "write_parquet(" not in batch_source
    assert not (spark_dir / "spark_realtime_bronze_entrypoint.py").exists()


def test_flink_feature_path_is_native_kafka_state_and_iceberg():
    flink_dir = Path("apps/data-platform/src/feature_engineering/flink")
    sources = "\n".join(path.read_text(encoding="utf-8") for path in flink_dir.glob("*.py"))
    stream_source = (flink_dir / "realtime_stream_job.py").read_text(encoding="utf-8")
    assert "import pandas" not in sources
    assert "pd." not in sources
    assert "KafkaSource.builder()" in stream_source
    assert "KafkaConsumer" not in stream_source
    assert "from_collection([0]" not in stream_source
    assert "--offline-store-enabled" in stream_source
    assert "StreamTableEnvironment" in stream_source


def test_offline_feature_drift_calculates_psi_for_shifted_distribution():
    score = calculate_psi([1, 1, 2, 2, 3, 3, 4, 4], [10, 10, 11, 11, 12, 12, 13, 13], buckets=4)

    assert score > 0.15


def test_pipeline_arg_parser_and_default_retrain_arguments():
    parsed = parse_pipeline_args(["source_run_path=s3a://lake/raw/run1", "training_percent=0.02"])
    defaults = default_pipeline_arguments("run-1")

    assert parsed["source_run_path"] == "s3a://lake/raw/run1"
    assert defaults["pipeline_run_id"] == "retrain-run-1"
    assert defaults["ray_job_name"] == "recsys-bst-ray-retrain-run-1"
    assert defaults["split_output_dir"].endswith("/retrain-run-1/ml/bst_split")


def test_trigger_retrain_skips_when_drift_passes(tmp_path):
    report = tmp_path / "drift.json"
    report.write_text(json.dumps({"run_id": "run-1", "passed": True, "features": []}), encoding="utf-8")

    result = trigger_retrain(str(report), "http://kfp", "exp", "pipeline.yaml", pushgateway_url=None)

    assert result.triggered is False
    assert result.reason == "drift_passed"


def test_trigger_retrain_calls_kfp_when_drift_fails(monkeypatch, tmp_path):
    report = tmp_path / "drift.json"
    report.write_text(
        json.dumps(
            {
                "run_id": "run-2",
                "passed": False,
                "features": [{"feature_table": "item_features", "feature": "views_1h", "passed": False}],
            }
        ),
        encoding="utf-8",
    )

    class Experiment:
        experiment_id = "experiment-1"

    class Run:
        run_id = "run-kfp-1"

    class Client:
        def __init__(self, host):
            assert host == "http://kfp"

        def create_experiment(self, name):
            assert name == "exp"
            return Experiment()

        def create_run_from_pipeline_package(self, **kwargs):
            assert kwargs["pipeline_file"] == "pipeline.yaml"
            assert kwargs["run_name"] == "recsys-drift-retrain-run-2"
            assert kwargs["arguments"]["pipeline_run_id"] == "retrain-run-2"
            assert kwargs["arguments"]["source_run_path"] == "s3a://lake/raw/run2"
            return Run()

    monkeypatch.setitem(__import__("sys").modules, "kfp", type("Kfp", (), {"Client": Client}))
    result = trigger_retrain(
        str(report),
        "http://kfp",
        "exp",
        "pipeline.yaml",
        pushgateway_url=None,
        pipeline_arguments={"source_run_path": "s3a://lake/raw/run2"},
    )

    assert failed_features(json.loads(report.read_text(encoding="utf-8"))) == ["item_features.views_1h"]
    assert result.triggered is True
    assert result.kfp_run_id == "run-kfp-1"


def test_trigger_retrain_kfp_error_is_non_blocking(monkeypatch, tmp_path):
    report = tmp_path / "drift.json"
    report.write_text(
        json.dumps(
            {
                "run_id": "run-3",
                "passed": False,
                "features": [{"feature_table": "item_features", "feature": "views_1h", "passed": False}],
            }
        ),
        encoding="utf-8",
    )

    class Client:
        def __init__(self, host):
            pass

        def create_experiment(self, name):
            raise RuntimeError("kfp unavailable")

    monkeypatch.setitem(__import__("sys").modules, "kfp", type("Kfp", (), {"Client": Client}))
    result = trigger_retrain(str(report), "http://kfp", "exp", "pipeline.yaml", pushgateway_url=None)

    assert result.triggered is False
    assert result.reason == "feature_drift"
    assert result.error == "kfp unavailable"
