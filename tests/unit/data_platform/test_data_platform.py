from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from features.flink.candidate_pool_job import candidate_updates
from features.flink.item_features_job import ItemFeatureState
from features.flink.realtime_stream_job import (
    StreamQualityTracker,
    build_postgres_feast_rows,
    build_offline_feature_rows,
    build_realtime_feature_payloads,
    normalize_event,
    parse_message,
)
from features.flink.user_aggregate_job import UserAggregateState
from features.flink.user_sequence_job import UserSequenceState
from feature_store.online_writer import RedisKeyTemplate, RedisOnlineWriter, dumps_feature_payload
from local.run_batch_features import main as run_batch_features_main
from local.run_batch_features import run_batch_features
from ingest.debezium import extract_debezium_after
from ingest.batch_lakehouse_ingestion import (
    LakehouseParquetLayout,
    infer_run_id,
    load_generator_run_to_lakehouse,
)
from lakehouse.iceberg import IcebergCatalogConfig, create_flink_catalog_sql, spark_iceberg_conf
from lakehouse.iceberg import RAW_GENERATOR_TABLES
from mlops.trigger_kubeflow_retrain import default_pipeline_arguments, failed_features, parse_pipeline_args, trigger_retrain
from monitoring.pushgateway import MetricSample, push_metrics
from validate.offline_feature_drift import calculate_psi, run_offline_feature_drift


def test_debezium_after_extraction_skips_deletes():
    assert extract_debezium_after({"payload": {"op": "d", "after": {"event_id": "e1"}}}) is None
    assert extract_debezium_after({"payload": {"op": "t", "after": {"event_id": "e1"}}}) is None
    after = extract_debezium_after({"payload": {"op": "c", "after": {"event_id": "e2"}}})
    assert after == {"event_id": "e2"}
    assert extract_debezium_after({"schema": {}, "payload": {"op": "c", "after": None}}) is None
    assert extract_debezium_after({"event_id": "raw"}) == {"event_id": "raw"}
    assert parse_message(b'{"payload":{"op":"c","after":{"event_id":"e3"}}}') == {"event_id": "e3"}


def test_batch_ingestion_uri_helpers_and_column_replacement(monkeypatch):
    import pyarrow as pa
    import pyarrow.fs as pafs
    import ingest.batch_lakehouse_ingestion as ingestion

    assert infer_run_id("/runs/source-1/") == "source-1"
    assert infer_run_id("/runs/source-1", "manual") == "manual"
    assert ingestion._normalise_uri("s3a://bucket/raw") == "s3://bucket/raw"

    monkeypatch.setenv("MINIO_ENDPOINT", "minio:9000")
    assert ingestion._s3_endpoint() == ("http", "minio:9000")
    monkeypatch.setenv("MINIO_ENDPOINT", "https://minio.example:9443")
    assert ingestion._s3_endpoint() == ("https", "minio.example:9443")

    filesystem, path = ingestion._filesystem_and_path(str(Path("warehouse") / "table"))
    assert isinstance(filesystem, pafs.LocalFileSystem)
    assert path.endswith("warehouse/table")
    with pytest.raises(ValueError, match="Unsupported lakehouse URI scheme"):
        ingestion._filesystem_and_path("gs://bucket/table")

    table = pa.table({"source_run_id": ["old"], "value": [1]})
    enriched = ingestion._enrich_table(table, source_run_id="run-2", ingestion_ts=ingestion.datetime.now(ingestion.timezone.utc))
    assert enriched.column("source_run_id").to_pylist() == ["run-2"]
    assert ingestion._part_name("behavior_events", "raw/run id!") == "part-raw-run-id-behavior_events.parquet"

    class MissingDirFilesystem:
        def delete_dir(self, path):
            raise FileNotFoundError(path)

    ingestion._delete_dir_if_exists(MissingDirFilesystem(), "missing")


def test_python_batch_ingestion_writes_parquet_lakehouse_layout(tmp_path):
    import pyarrow as pa
    import pyarrow.parquet as pq

    run_path = tmp_path / "raw" / "test_run"
    for table_name in RAW_GENERATOR_TABLES:
        table_path = run_path / table_name
        table_path.mkdir(parents=True)
        pq.write_table(pa.table({"id": [1], "value": [table_name]}), table_path / "part-00000.parquet")

    layout = LakehouseParquetLayout(warehouse_uri=str(tmp_path / "warehouse"), namespace="lakehouse")
    counts = load_generator_run_to_lakehouse(run_path, layout=layout, mode="overwrite")

    assert counts == {table_name: 1 for table_name in RAW_GENERATOR_TABLES}
    output = pq.read_table(tmp_path / "warehouse" / "lakehouse" / "behavior_events")
    assert output.column("source_run_id").to_pylist() == ["test_run"]
    assert "lakehouse_ingestion_ts" in output.column_names


def test_python_batch_ingestion_cli_delegates_to_loader(monkeypatch, capsys):
    import ingest.batch_lakehouse_ingestion as ingestion

    captured = {}

    def fake_load_generator_run_to_lakehouse(run_path, *, layout, mode, run_id):
        captured["run_path"] = run_path
        captured["layout"] = layout
        captured["mode"] = mode
        captured["run_id"] = run_id
        return {"behavior_events": 2}

    monkeypatch.setattr(ingestion, "load_generator_run_to_lakehouse", fake_load_generator_run_to_lakehouse)
    monkeypatch.setattr(
        "sys.argv",
        [
            "batch_lakehouse_ingestion",
            "--run-path",
            "raw/run",
            "--run-id",
            "explicit-run",
            "--mode",
            "append",
            "--lakehouse-warehouse",
            "s3a://lake/warehouse",
            "--iceberg-lakehouse-namespace",
            "bronze",
        ],
    )

    assert ingestion.main() == 0
    assert captured["run_path"] == "raw/run"
    assert captured["layout"] == LakehouseParquetLayout("s3a://lake/warehouse", "bronze")
    assert captured["mode"] == "append"
    assert captured["run_id"] == "explicit-run"
    assert json.loads(capsys.readouterr().out) == {"behavior_events": 2}


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

    postgres_rows = build_postgres_feast_rows(event, sequence, aggregate, item)
    assert postgres_rows["user_sequence_features"][0]["hist_item_ids"] == [10]
    assert postgres_rows["user_sequence_features"][0]["hist_length"] == 1
    assert postgres_rows["user_aggregate_features"][0]["carts_30m"] == 1
    assert postgres_rows["item_features"][0]["conversion_rate_7d"] == item["conversion_rate_7d"]


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


def test_online_writer_writes_all_feature_key_templates():
    class Redis:
        def __init__(self):
            self.calls = []

        def set(self, key, value, ex):
            self.calls.append((key, json.loads(value), ex))

    redis = Redis()
    writer = RedisOnlineWriter(redis, RedisKeyTemplate(user_sequence="seq:{user_id}", user_aggregate="agg:{user_id}", item_features="item:{product_id}"))

    assert writer.write_user_sequence(7, {"items": [1, float("nan")]}, 60) == "seq:7"
    assert writer.write_user_aggregate(7, {"views": 2}, 120) == "agg:7"
    assert writer.write_item_features(9, {"score": 0.5}, 180) == "item:9"
    assert redis.calls == [
        ("seq:7", {"items": [1, None]}, 60),
        ("agg:7", {"views": 2}, 120),
        ("item:9", {"score": 0.5}, 180),
    ]


def test_local_batch_runner_delegates_to_spark_entrypoint(monkeypatch, capsys):
    import local.run_batch_features as module

    captured = {}

    def fake_run_pyspark_batch(config_path):
        captured["config_path"] = config_path
        return {"silver": 2, "features": 3}

    monkeypatch.setattr(module, "run_pyspark_batch", fake_run_pyspark_batch)

    assert run_batch_features("config.yaml") == {"silver": 2, "features": 3}
    assert captured["config_path"] == "config.yaml"

    monkeypatch.setattr("sys.argv", ["run_batch_features", "--config", "cli.yaml"])
    assert run_batch_features_main() == 0
    assert '"features": 3' in capsys.readouterr().out


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
    spark_dir = Path("apps/data-platform/src/features/spark")
    sources = "\n".join(path.read_text(encoding="utf-8") for path in spark_dir.glob("*.py"))
    batch_source = (spark_dir / "spark_batch_entrypoint.py").read_text(encoding="utf-8")
    assert "import pandas" not in sources
    assert "pd." not in sources
    assert "from pyspark.sql" in sources
    assert 'source", os.getenv("SPARK_BATCH_SOURCE", "lakehouse")' in batch_source
    assert "write_iceberg_table" in batch_source
    assert "feast_offline_store_uri" in batch_source
    assert "write_parquet(" in batch_source
    assert not (spark_dir / "spark_realtime_bronze_entrypoint.py").exists()


def test_spark_batch_postgres_export_config_supports_explicit_config_and_env(monkeypatch):
    import features.spark.spark_batch_entrypoint as spark_batch

    explicit = spark_batch._postgres_export_config(
        {
            "feast_postgres_export": {
                "enabled": True,
                "host": "feature-postgres-a",
                "port": 5433,
                "database": "features_a",
                "schema": "schema_a",
                "user": "feast_a",
                "password": "secret-a",
                "sslmode": "disable",
            }
        }
    )
    assert explicit["enabled"] is True
    assert explicit["config"].host == "feature-postgres-a"
    assert explicit["config"].port == 5433
    assert explicit["config"].database == "features_a"
    assert explicit["config"].schema == "schema_a"
    assert explicit["config"].user == "feast_a"

    monkeypatch.setenv("FEAST_POSTGRES_EXPORT_ENABLED", "1")
    monkeypatch.setenv("FEAST_POSTGRES_HOST", "feature-postgres-b")
    monkeypatch.setenv("FEAST_POSTGRES_PORT", "5434")
    monkeypatch.setenv("FEAST_POSTGRES_DB", "features_b")
    monkeypatch.setenv("FEAST_POSTGRES_SCHEMA", "schema_b")
    monkeypatch.setenv("FEAST_POSTGRES_USER", "feast_b")
    monkeypatch.setenv("FEAST_POSTGRES_PASSWORD", "secret-b")

    from_env = spark_batch._postgres_export_config({})
    assert from_env["enabled"] is True
    assert from_env["config"].host == "feature-postgres-b"
    assert from_env["config"].port == 5434
    assert from_env["config"].database == "features_b"
    assert from_env["config"].schema == "schema_b"
    assert from_env["config"].user == "feast_b"


def test_flink_feature_path_is_native_kafka_state_and_iceberg():
    flink_dir = Path("apps/data-platform/src/features/flink")
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


def test_offline_feature_drift_reads_sampled_parquet_baseline_without_spark(tmp_path):
    import pandas as pd

    baseline = tmp_path / "baseline" / "item_features"
    current = tmp_path / "current" / "item_features"
    baseline.mkdir(parents=True)
    current.mkdir(parents=True)
    pd.DataFrame(
        {
            "item_id": range(60),
            "views_1h": [1 + index % 4 for index in range(60)],
            "popularity_score": [0.1 + index * 0.001 for index in range(60)],
        }
    ).to_parquet(baseline / "part-00000.parquet", index=False)
    pd.DataFrame(
        {
            "item_id": range(60),
            "views_1h": [100 + index % 4 for index in range(60)],
            "popularity_score": [0.9 + index * 0.001 for index in range(60)],
        }
    ).to_parquet(current / "part-00000.parquet", index=False)

    report = run_offline_feature_drift(
        "run-psi",
        str(tmp_path / "report.json"),
        feature_tables=["item_features"],
        current_feature_root=str(tmp_path / "current"),
        baseline_path=str(tmp_path / "baseline"),
        threshold=0.15,
        sample_rows=20,
        pushgateway_url=None,
        bootstrap_baseline=False,
    )

    failed = {f"{item['feature_table']}.{item['feature']}" for item in report["features"] if not item["passed"]}
    assert report["passed"] is False
    assert "item_features.views_1h" in failed
    assert report["features"][0]["feature_view"] == "item_features"
    assert "spark" not in report["drift_engine"].lower()


def test_offline_feature_drift_bootstraps_missing_reference_baseline(tmp_path):
    import pandas as pd

    current = tmp_path / "current" / "item_features"
    current.mkdir(parents=True)
    pd.DataFrame({"item_id": [1, 2, 3], "views_1h": [1.0, 2.0, 3.0]}).to_parquet(
        current / "part-00000.parquet",
        index=False,
    )

    report = run_offline_feature_drift(
        "run-bootstrap",
        str(tmp_path / "report.json"),
        feature_tables=["item_features"],
        current_feature_root=str(tmp_path / "current"),
        baseline_path=str(tmp_path / "baseline"),
        pushgateway_url=None,
        bootstrap_baseline=True,
    )

    assert report["passed"] is True
    assert report["baseline_bootstrapped"] == ["item_features"]
    assert (tmp_path / "baseline" / "item_features" / "part-run-bootstrap.parquet").exists()


def test_pipeline_arg_parser_and_default_retrain_arguments():
    parsed = parse_pipeline_args(["source_run_path=s3a://lake/raw/run1", "training_percent=0.02"])
    defaults = default_pipeline_arguments("run-1")

    assert parsed["source_run_path"] == "s3a://lake/raw/run1"
    assert defaults["pipeline_run_id"] == "retrain-run-1"
    assert defaults["ray_job_name"].startswith("recsys-bst-ray-retrain-run-1-")
    assert len(defaults["ray_job_name"]) <= 47
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


def test_pushgateway_connection_reset_is_non_blocking(monkeypatch):
    def fail_urlopen(*args, **kwargs):
        raise ConnectionResetError("reset")

    monkeypatch.setattr("urllib.request.urlopen", fail_urlopen)

    assert push_metrics([MetricSample("recsys_test_metric", 1.0)], "recsys_test", gateway_url="http://pushgateway") is False
