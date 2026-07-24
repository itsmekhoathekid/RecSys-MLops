from __future__ import annotations

import asyncio
import json
import sys
from types import ModuleType
from pathlib import Path
from types import SimpleNamespace

import pytest

from features.flink.features.candidate_pool import (
    candidate_updates,
    refresh_user_candidate_pool,
)
from features.flink.feature_windows import (
    EarlyAndEventTimeTrigger,
    add_event_to_feature_pane,
    attach_pane_metadata,
    build_item_feature_update,
    build_user_feature_update,
    create_feature_pane_accumulator,
    feature_pane_result,
)
from features.flink.operators.row_mappers import (
    build_offline_item_feature_rows,
    build_offline_user_feature_rows,
    build_postgres_item_feature_rows,
    build_postgres_user_feature_rows,
    build_stream_behavior_row,
)
from features.flink.sinks.postgres_async import postgres_async_capacity
from features.flink.source import (
    normalize_event,
    parse_message,
)
from features.flink.stream_config import (
    StreamConfig,
    parse_stream_args,
    stream_pipeline_role,
)
from features.flink.sinks.rate_limit import (
    AsyncTokenBucketRateLimiter,
    TokenBucketRateLimiter,
)
from feature_store.online_writer import (
    RedisKeyTemplate,
    RedisOnlineWriter,
    dumps_feature_payload,
)
from local.run_batch_features import main as run_batch_features_main
from local.run_batch_features import run_batch_features
from ingest.debezium import extract_debezium_after
from ingest.batch_lakehouse_ingestion import (
    LakehouseIcebergLayout,
    infer_run_id,
    load_generator_run_to_lakehouse,
)
from lakehouse.iceberg import (
    IcebergCatalogConfig,
    create_flink_catalog_sql,
    spark_iceberg_conf,
)
from lakehouse.iceberg import RAW_GENERATOR_TABLES
from mlops.trigger_kubeflow_retrain import (
    default_pipeline_arguments,
    failed_features,
    parse_pipeline_args,
    trigger_retrain,
)
from monitoring.pushgateway import MetricSample, push_metrics
from validate.offline_feature_drift import calculate_psi, run_offline_feature_drift


def test_debezium_after_extraction_skips_deletes():
    assert (
        extract_debezium_after({"payload": {"op": "d", "after": {"event_id": "e1"}}})
        is None
    )
    assert (
        extract_debezium_after({"payload": {"op": "t", "after": {"event_id": "e1"}}})
        is None
    )
    after = extract_debezium_after(
        {"payload": {"op": "c", "after": {"event_id": "e2"}}}
    )
    assert after == {"event_id": "e2"}
    assert (
        extract_debezium_after({"schema": {}, "payload": {"op": "c", "after": None}})
        is None
    )
    assert extract_debezium_after({"event_id": "raw"}) == {"event_id": "raw"}
    assert parse_message(b'{"payload":{"op":"c","after":{"event_id":"e3"}}}') == {
        "event_id": "e3"
    }


def test_batch_ingestion_uri_helpers(monkeypatch):
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

    layout = LakehouseIcebergLayout("recsys", "lakehouse")
    assert (
        layout.table_name("behavior_events")
        == "recsys.lakehouse.bronze_behavior_events"
    )


def test_batch_ingestion_commits_every_generator_table_as_bronze_iceberg(monkeypatch):
    import pyspark.sql.functions as functions
    import ingest.batch_lakehouse_ingestion as ingestion

    class Expression:
        def cast(self, _type):
            return self

    class Frame:
        def withColumn(self, _name, _value):
            return self

    written = []
    monkeypatch.setattr(functions, "lit", lambda _value: Expression())
    monkeypatch.setattr(
        ingestion, "create_spark_namespace", lambda spark, catalog: None
    )
    monkeypatch.setattr(
        ingestion, "read_parquet_table", lambda spark, run_path, table: Frame()
    )
    monkeypatch.setattr(ingestion, "row_count", lambda frame: 1)
    monkeypatch.setattr(
        ingestion,
        "write_iceberg_table",
        lambda frame, table_name, mode: written.append((table_name, mode)),
    )

    counts = load_generator_run_to_lakehouse(
        "/raw/test_run",
        spark=object(),
        layout=LakehouseIcebergLayout("recsys", "lakehouse"),
        mode="overwrite",
    )

    assert counts == {table_name: 1 for table_name in RAW_GENERATOR_TABLES}
    assert written == [
        (f"recsys.lakehouse.bronze_{table}", "overwrite")
        for table in RAW_GENERATOR_TABLES
    ]


def test_python_batch_ingestion_cli_delegates_to_loader(monkeypatch, capsys):
    import ingest.batch_lakehouse_ingestion as ingestion

    captured = {}

    def fake_load_generator_run_to_lakehouse(run_path, *, catalog, mode, run_id):
        captured["run_path"] = run_path
        captured["catalog"] = catalog
        captured["mode"] = mode
        captured["run_id"] = run_id
        return {"behavior_events": 2}

    monkeypatch.setattr(
        ingestion,
        "load_generator_run_to_lakehouse",
        fake_load_generator_run_to_lakehouse,
    )
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
    assert captured["catalog"].warehouse_uri == "s3a://lake/warehouse"
    assert captured["catalog"].lakehouse_namespace == "bronze"
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


@pytest.mark.parametrize(
    (
        "disable_offline_store",
        "disable_online_store",
        "offline_store_enabled",
        "expected",
    ),
    [
        (True, False, False, "online"),
        (False, True, True, "offline"),
        (False, False, True, "hybrid"),
        (False, True, False, "disabled"),
    ],
)
def test_stream_pipeline_role_separates_duplicate_consumers(
    disable_offline_store,
    disable_online_store,
    offline_store_enabled,
    expected,
):
    args = SimpleNamespace(
        disable_offline_store=disable_offline_store,
        disable_online_store=disable_online_store,
        offline_store_enabled=offline_store_enabled,
    )

    assert stream_pipeline_role(args) == expected


def test_stream_parser_returns_typed_config_and_applies_store_policy():
    config = parse_stream_args(
        ["--group-id", "typed-config-test", "--disable-offline-store"]
    )

    assert isinstance(config, StreamConfig)
    assert config.group_id == "typed-config-test"
    assert config.offline_store_enabled is False


def _pane_snapshot(events, *, kind, entity_id, start_ms, end_ms, watermark_ms=-1):
    accumulator = create_feature_pane_accumulator()
    for event in events:
        add_event_to_feature_pane(event, accumulator)
    return attach_pane_metadata(
        feature_pane_result(accumulator),
        kind=kind,
        entity_id=entity_id,
        window_start_ms=start_ms,
        window_end_ms=end_ms,
        current_watermark_ms=watermark_ms,
    )


def test_feature_window_trigger_fires_early_final_and_accepted_late(monkeypatch):
    class FakeDescriptor:
        def __init__(self, name, value_type):
            self.name = name
            self.value_type = value_type

    class FakeTrigger:
        pass

    class FakeResult:
        CONTINUE = "continue"
        FIRE = "fire"

    pyflink = ModuleType("pyflink")
    common = ModuleType("pyflink.common")
    common.Types = SimpleNamespace(LONG=lambda: "long", BOOLEAN=lambda: "boolean")
    datastream = ModuleType("pyflink.datastream")
    state = ModuleType("pyflink.datastream.state")
    state.ValueStateDescriptor = FakeDescriptor
    window_module = ModuleType("pyflink.datastream.window")
    window_module.Trigger = FakeTrigger
    window_module.TriggerResult = FakeResult
    monkeypatch.setitem(sys.modules, "pyflink", pyflink)
    monkeypatch.setitem(sys.modules, "pyflink.common", common)
    monkeypatch.setitem(sys.modules, "pyflink.datastream", datastream)
    monkeypatch.setitem(sys.modules, "pyflink.datastream.state", state)
    monkeypatch.setitem(sys.modules, "pyflink.datastream.window", window_module)

    class TimerState:
        def __init__(self):
            self.current = None

        def value(self):
            return self.current

        def update(self, value):
            self.current = value

        def clear(self):
            self.current = None

    class Context:
        def __init__(self, watermark=0):
            self.watermark = watermark
            self.processing_time = 1_000
            self.states = {}
            self.event_timers = []
            self.processing_timers = []

        def get_current_watermark(self):
            return self.watermark

        def get_current_processing_time(self):
            return self.processing_time

        def get_partitioned_state(self, descriptor):
            return self.states.setdefault(descriptor.name, TimerState())

        def register_event_time_timer(self, timestamp):
            self.event_timers.append(timestamp)

        def delete_event_time_timer(self, timestamp):
            if timestamp in self.event_timers:
                self.event_timers.remove(timestamp)

        def register_processing_time_timer(self, timestamp):
            self.processing_timers.append(timestamp)

        def delete_processing_time_timer(self, timestamp):
            if timestamp in self.processing_timers:
                self.processing_timers.remove(timestamp)

    window = SimpleNamespace(max_timestamp=lambda: 59_999)
    trigger = EarlyAndEventTimeTrigger(5, "test-early-timer")
    context = Context()
    assert trigger.on_element({}, 1_000, window, context) == FakeResult.CONTINUE
    assert context.event_timers == [59_999]
    assert context.states["test-early-timer"].value() == 6_000
    assert trigger.on_processing_time(6_000, window, context) == FakeResult.FIRE
    assert context.states["test-early-timer"].value() is None
    assert trigger.on_processing_time(11_000, window, context) == FakeResult.CONTINUE

    context.processing_time = 7_000
    assert trigger.on_element({}, 7_000, window, context) == FakeResult.CONTINUE
    assert context.states["test-early-timer"].value() == 12_000

    context.watermark = 59_999
    assert trigger.on_event_time(59_999, window, context) == FakeResult.FIRE
    assert context.states["test-early-timer"].value() is None
    assert trigger.on_element({}, 2_000, window, context) == FakeResult.FIRE


def test_flink_time_window_updates_feed_online_and_offline_rows():
    first = normalize_event(
        {
            "event_id": "e1",
            "user_id": "1",
            "product_id": "10",
            "event_type": "view",
            "event_timestamp": "2026-01-01T00:00:01Z",
            "category_id": 2,
            "brand_id": 3,
            "price_bucket": 4,
            "price": 9.0,
        }
    )
    second = normalize_event(
        {
            "event_id": "e2",
            "user_id": "1",
            "product_id": "10",
            "event_type": "cart",
            "event_timestamp": "2026-01-01T00:00:08Z",
            "category_id": 2,
            "brand_id": 3,
            "price_bucket": 4,
            "price": 9.0,
        }
    )
    assert first is not None and second is not None
    start_ms = 1_767_225_600_000
    pane = _pane_snapshot(
        [first, second],
        kind="user",
        entity_id=1,
        start_ms=start_ms,
        end_ms=start_ms + 60_000,
    )
    user_panes, user_update = build_user_feature_update(
        {},
        pane,
        max_history_length=50,
        retention_seconds=7 * 24 * 60 * 60,
    )
    item_panes, item_update = build_item_feature_update(
        {},
        {**pane, "kind": "item", "entity_id": 10},
        retention_seconds=7 * 24 * 60 * 60,
    )
    assert user_update is not None and item_update is not None
    assert len(user_panes) == 1 and len(item_panes) == 1
    assert user_update["sequence_payload"]["item_ids"] == [10, 10]
    assert user_update["aggregate_payload"]["views_30m"] == 1
    assert user_update["aggregate_payload"]["carts_30m"] == 1
    assert item_update["item_payload"]["views_1h"] == 1
    assert item_update["item_payload"]["carts_1h"] == 1
    assert user_update["sequence_payload"]["is_final"] is False

    behavior = build_stream_behavior_row(second, "cdc.behavior_events", 60)
    offline_user = build_offline_user_feature_rows(user_update)
    offline_item = build_offline_item_feature_rows(item_update)
    postgres_user = build_postgres_user_feature_rows(user_update)
    postgres_item = build_postgres_item_feature_rows(item_update)
    assert behavior["event_id"] == "e2"
    assert offline_user["stream_user_sequence_features"][0]["sequence_length"] == 2
    assert offline_item["stream_item_features"][0]["popularity_score"] == 4.0
    assert postgres_user["user_sequence_features"][0]["hist_item_ids"] == [10, 10]
    assert postgres_user["user_aggregate_features"][0]["carts_30m"] == 1
    assert postgres_item["item_features"][0]["source_event_id"] == "e2"
    assert (
        "candidate:trending:1h",
        10,
        item_update["item_payload"]["views_1h"]
        + item_update["item_payload"]["carts_1h"] * 3.0,
    ) in candidate_updates(item_update["item_payload"])


def test_flink_pane_revisions_do_not_double_count_and_prune_after_seven_days():
    first = normalize_event(
        {
            "event_id": "e1",
            "user_id": "1",
            "product_id": "10",
            "event_type": "view",
            "event_timestamp": "2026-01-01T00:00:01Z",
        }
    )
    second = normalize_event(
        {
            "event_id": "e2",
            "user_id": "1",
            "product_id": "10",
            "event_type": "cart",
            "event_timestamp": "2026-01-01T00:00:08Z",
        }
    )
    future = normalize_event(
        {
            "event_id": "e3",
            "user_id": "1",
            "product_id": "11",
            "event_type": "purchase",
            "event_timestamp": "2026-01-09T00:00:01Z",
        }
    )
    assert first is not None and second is not None and future is not None
    start_ms = 1_767_225_600_000
    early = _pane_snapshot(
        [first], kind="user", entity_id=1, start_ms=start_ms, end_ms=start_ms + 60_000
    )
    revision = _pane_snapshot(
        [first, second, {**second, "_is_duplicate": True}],
        kind="user",
        entity_id=1,
        start_ms=start_ms,
        end_ms=start_ms + 60_000,
        watermark_ms=start_ms + 60_000,
    )
    panes, _ = build_user_feature_update(
        {},
        early,
        max_history_length=50,
        retention_seconds=7 * 24 * 60 * 60,
    )
    unchanged_panes, unchanged_update = build_user_feature_update(
        panes,
        early,
        max_history_length=50,
        retention_seconds=7 * 24 * 60 * 60,
    )
    item_panes, first_item_update = build_item_feature_update(
        {},
        {**early, "kind": "item", "entity_id": 10},
        retention_seconds=7 * 24 * 60 * 60,
    )
    unchanged_item_panes, repeated_item_update = build_item_feature_update(
        item_panes,
        {**early, "kind": "item", "entity_id": 10},
        retention_seconds=7 * 24 * 60 * 60,
    )
    assert unchanged_panes == panes
    assert unchanged_update is None
    assert first_item_update is not None
    assert unchanged_item_panes == item_panes
    assert repeated_item_update is None
    panes, revision_update = build_user_feature_update(
        panes,
        revision,
        max_history_length=50,
        retention_seconds=7 * 24 * 60 * 60,
    )
    assert revision_update is not None
    assert len(panes) == 1
    assert revision_update["sequence_payload"]["sequence_length"] == 2
    assert revision_update["aggregate_payload"]["views_30m"] == 1
    assert revision_update["aggregate_payload"]["carts_30m"] == 1
    assert revision_update["is_final"] is True

    future_start_ms = start_ms + 8 * 24 * 60 * 60 * 1000
    future_pane = _pane_snapshot(
        [future],
        kind="user",
        entity_id=1,
        start_ms=future_start_ms,
        end_ms=future_start_ms + 60_000,
    )
    panes, future_update = build_user_feature_update(
        panes,
        future_pane,
        max_history_length=50,
        retention_seconds=7 * 24 * 60 * 60,
    )
    assert future_update is not None
    assert list(panes) == [future_start_ms]
    assert future_update["sequence_payload"]["item_ids"] == [11]


def test_flink_rolling_horizons_use_event_time_boundaries():
    old = normalize_event(
        {
            "event_id": "old",
            "user_id": "1",
            "product_id": "10",
            "event_type": "view",
            "event_timestamp": "2026-01-01T00:00:00Z",
            "category_id": 1,
        }
    )
    current = normalize_event(
        {
            "event_id": "current",
            "user_id": "1",
            "product_id": "10",
            "event_type": "cart",
            "event_timestamp": "2026-01-01T01:01:00Z",
            "category_id": 2,
        }
    )
    assert old is not None and current is not None
    first = _pane_snapshot(
        old and [old], kind="user", entity_id=1, start_ms=0, end_ms=60_000
    )
    latest = _pane_snapshot(
        [current], kind="user", entity_id=1, start_ms=3_660_000, end_ms=3_720_000
    )
    panes, _ = build_user_feature_update(
        {}, first, max_history_length=50, retention_seconds=7 * 24 * 60 * 60
    )
    _, user_update = build_user_feature_update(
        panes, latest, max_history_length=50, retention_seconds=7 * 24 * 60 * 60
    )
    item_panes, _ = build_item_feature_update(
        {},
        {**first, "kind": "item", "entity_id": 10},
        retention_seconds=7 * 24 * 60 * 60,
    )
    _, item_update = build_item_feature_update(
        item_panes,
        {**latest, "kind": "item", "entity_id": 10},
        retention_seconds=7 * 24 * 60 * 60,
    )
    assert user_update is not None and item_update is not None
    assert user_update["aggregate_payload"]["views_30m"] == 0
    assert user_update["aggregate_payload"]["carts_30m"] == 1
    assert user_update["aggregate_payload"]["distinct_categories_7d"] == 2
    assert item_update["item_payload"]["views_1h"] == 0
    assert item_update["item_payload"]["views_24h"] == 1
    assert item_update["item_payload"]["carts_1h"] == 1


def test_refresh_user_candidate_pool_merges_category_candidates_with_ttl():
    class Redis:
        def __init__(self):
            self.calls = []

        def zrevrange(self, key, start, end, withscores=False):
            self.calls.append(("zrevrange", key, start, end, withscores))
            return [(b"10", 9.5), ("11", 8.0)]

        def zadd(self, key, values):
            self.calls.append(("zadd", key, values))

        def zremrangebyrank(self, key, start, end):
            self.calls.append(("zremrangebyrank", key, start, end))

        def expire(self, key, seconds):
            self.calls.append(("expire", key, seconds))

    redis = Redis()

    assert (
        refresh_user_candidate_pool(
            redis, user_id=7, category_id=2, limit=2, ttl_seconds=60
        )
        == 2
    )
    assert redis.calls == [
        ("zrevrange", "candidate:popular:category:2", 0, 1, True),
        ("zadd", "candidate:user:7", {"10": 9.5, "11": 8.0}),
        ("zremrangebyrank", "candidate:user:7", 0, -3),
        ("expire", "candidate:user:7", 60),
    ]


def test_sink_rate_limiter_applies_backpressure_without_buffering_events():
    now = [0.0]
    sleeps = []

    def clock():
        return now[0]

    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds

    limiter = TokenBucketRateLimiter(2, burst_events=2, clock=clock, sleeper=sleep)
    assert limiter.acquire() == 0.0
    assert limiter.acquire() == 0.0
    assert limiter.acquire() == pytest.approx(0.5)
    assert sleeps == [pytest.approx(0.5)]


def test_zero_sink_rate_disables_rate_limiting():
    limiter = TokenBucketRateLimiter(
        0, burst_events=1, sleeper=lambda _seconds: pytest.fail("slept")
    )
    assert limiter.enabled is False
    assert limiter.acquire() == 0.0


def test_async_sink_rate_limiter_waits_without_blocking_worker(monkeypatch):
    now = [0.0]
    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds

    monkeypatch.setattr("features.flink.sinks.rate_limit.asyncio.sleep", fake_sleep)
    limiter = AsyncTokenBucketRateLimiter(2, burst_events=1, clock=lambda: now[0])

    async def consume():
        return [await limiter.acquire(), await limiter.acquire()]

    assert asyncio.run(consume()) == [0.0, pytest.approx(0.5)]
    assert sleeps == [pytest.approx(0.5)]


def test_postgres_async_capacity_never_exceeds_connection_pool():
    assert (
        postgres_async_capacity(
            SimpleNamespace(async_io_capacity=64, postgres_async_pool_size=16)
        )
        == 16
    )
    assert (
        postgres_async_capacity(
            SimpleNamespace(async_io_capacity=8, postgres_async_pool_size=16)
        )
        == 8
    )


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
    writer = RedisOnlineWriter(
        redis,
        RedisKeyTemplate(
            user_sequence="seq:{user_id}",
            user_aggregate="agg:{user_id}",
            item_features="item:{product_id}",
        ),
    )

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
    assert (
        config.lakehouse_table("behavior_events") == "recsys.lakehouse.behavior_events"
    )
    assert config.feature_database == "recsys_features.feature_store"
    assert (
        config.feature_table("item_features")
        == "recsys_features.feature_store.item_features"
    )
    spark_conf = spark_iceberg_conf(config)
    assert (
        spark_conf["spark.sql.catalog.recsys"]
        == "org.apache.iceberg.spark.SparkCatalog"
    )
    assert (
        spark_conf["spark.sql.catalog.recsys.warehouse"]
        == "s3a://recsys-lakehouse/warehouse"
    )
    assert (
        spark_conf["spark.sql.catalog.recsys_features.warehouse"]
        == "s3a://recsys-offline-feature-store/warehouse"
    )
    assert "CREATE CATALOG recsys" in create_flink_catalog_sql(config)


def test_spark_feature_path_is_native_iceberg_not_pandas_or_parquet_writer():
    spark_dir = Path("apps/data-platform/src/features/spark")
    sources = "\n".join(
        path.read_text(encoding="utf-8") for path in spark_dir.glob("*.py")
    )
    batch_source = (spark_dir / "spark_batch_entrypoint.py").read_text(encoding="utf-8")
    assert "import pandas" not in sources
    assert "pd." not in sources
    assert "from pyspark.sql" in sources
    assert (
        'source", os.getenv("SPARK_BATCH_SOURCE", "silver_lakehouse")' in batch_source
    )
    assert "write_iceberg_table" in batch_source
    assert "feast_offline_store_uri" in batch_source
    assert "write_parquet(" in batch_source
    assert not (spark_dir / "spark_realtime_bronze_entrypoint.py").exists()


def test_spark_batch_postgres_export_config_supports_explicit_config_and_env(
    monkeypatch,
):
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
    sources = "\n".join(
        path.read_text(encoding="utf-8") for path in flink_dir.rglob("*.py")
    )
    stream_source = (flink_dir / "realtime_stream_job.py").read_text(encoding="utf-8")
    kafka_source = (flink_dir / "source.py").read_text(encoding="utf-8")
    config_source = (flink_dir / "stream_config.py").read_text(encoding="utf-8")
    iceberg_source = (flink_dir / "sinks" / "iceberg.py").read_text(encoding="utf-8")
    feature_window_source = (flink_dir / "feature_windows.py").read_text(
        encoding="utf-8"
    )
    assert "import pandas" not in sources
    assert "pd." not in sources
    assert "KafkaSource.builder()" in kafka_source
    assert "KafkaConsumer" not in sources
    assert "from_collection([0]" not in sources
    assert "--offline-store-enabled" in config_source
    assert "StreamTableEnvironment" in iceberg_source
    assert "GlobalWindows" not in sources
    assert "TumblingEventTimeWindows.of" in feature_window_source
    assert 'name(f"{kind}-feature-event-time-panes")' in feature_window_source
    assert 'name(f"{kind}-feature-rolling-horizons")' in feature_window_source
    assert "--feature-window-seconds" in config_source
    assert "--feature-early-fire-seconds" in config_source
    assert "BuildUserFeatures" not in stream_source
    assert "BuildItemFeatures" not in stream_source
    assert "TriggerResult.FIRE" in feature_window_source
    assert "StreamQualityTracker" not in sources
    assert "continuous_feature_window_trigger" not in sources


def test_offline_feature_drift_calculates_psi_for_shifted_distribution():
    score = calculate_psi(
        [1, 1, 2, 2, 3, 3, 4, 4], [10, 10, 11, 11, 12, 12, 13, 13], buckets=4
    )

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

    failed = {
        f"{item['feature_table']}.{item['feature']}"
        for item in report["features"]
        if not item["passed"]
    }
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
    assert (
        tmp_path / "baseline" / "item_features" / "part-run-bootstrap.parquet"
    ).exists()


def test_pipeline_arg_parser_and_default_retrain_arguments():
    parsed = parse_pipeline_args(
        ["source_run_path=s3a://lake/raw/run1", "training_percent=0.02"]
    )
    defaults = default_pipeline_arguments("run-1")

    assert parsed["source_run_path"] == "s3a://lake/raw/run1"
    assert defaults["pipeline_run_id"] == "retrain-run-1"
    assert defaults["ray_job_name"].startswith("recsys-bst-ray-tune-retrain-run-1-")
    assert defaults["ray_train_job_name"].startswith(
        "recsys-bst-ray-ddp-retrain-run-1-"
    )
    assert len(defaults["ray_job_name"]) <= 47
    assert len(defaults["ray_train_job_name"]) <= 47
    assert defaults["split_output_dir"].endswith("/retrain-run-1/ml/bst_split")


def test_trigger_retrain_skips_when_drift_passes(tmp_path):
    report = tmp_path / "drift.json"
    report.write_text(
        json.dumps({"run_id": "run-1", "passed": True, "features": []}),
        encoding="utf-8",
    )

    result = trigger_retrain(
        str(report), "http://kfp", "exp", "pipeline.yaml", pushgateway_url=None
    )

    assert result.triggered is False
    assert result.reason == "drift_passed"


def test_trigger_retrain_calls_kfp_when_drift_fails(monkeypatch, tmp_path):
    report = tmp_path / "drift.json"
    report.write_text(
        json.dumps(
            {
                "run_id": "run-2",
                "passed": False,
                "features": [
                    {
                        "feature_table": "item_features",
                        "feature": "views_1h",
                        "passed": False,
                    }
                ],
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
            assert kwargs["arguments"]["ray_job_name"].startswith(
                "recsys-bst-ray-tune-retrain-run-2-"
            )
            assert kwargs["arguments"]["ray_train_job_name"].startswith(
                "recsys-bst-ray-ddp-retrain-run-2-"
            )
            assert kwargs["arguments"]["source_run_path"] == "s3a://lake/raw/run2"
            return Run()

    monkeypatch.setitem(
        __import__("sys").modules, "kfp", type("Kfp", (), {"Client": Client})
    )
    result = trigger_retrain(
        str(report),
        "http://kfp",
        "exp",
        "pipeline.yaml",
        pushgateway_url=None,
        pipeline_arguments={"source_run_path": "s3a://lake/raw/run2"},
    )

    assert failed_features(json.loads(report.read_text(encoding="utf-8"))) == [
        "item_features.views_1h"
    ]
    assert result.triggered is True
    assert result.kfp_run_id == "run-kfp-1"


def test_trigger_retrain_kfp_error_is_non_blocking(monkeypatch, tmp_path):
    report = tmp_path / "drift.json"
    report.write_text(
        json.dumps(
            {
                "run_id": "run-3",
                "passed": False,
                "features": [
                    {
                        "feature_table": "item_features",
                        "feature": "views_1h",
                        "passed": False,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    class Client:
        def __init__(self, host):
            pass

        def create_experiment(self, name):
            raise RuntimeError("kfp unavailable")

    monkeypatch.setitem(
        __import__("sys").modules, "kfp", type("Kfp", (), {"Client": Client})
    )
    result = trigger_retrain(
        str(report), "http://kfp", "exp", "pipeline.yaml", pushgateway_url=None
    )

    assert result.triggered is False
    assert result.reason == "feature_drift"
    assert result.error == "kfp unavailable"


def test_pushgateway_connection_reset_is_non_blocking(monkeypatch):
    def fail_urlopen(*args, **kwargs):
        raise ConnectionResetError("reset")

    monkeypatch.setattr("urllib.request.urlopen", fail_urlopen)

    assert (
        push_metrics(
            [MetricSample("recsys_test_metric", 1.0)],
            "recsys_test",
            gateway_url="http://pushgateway",
        )
        is False
    )
