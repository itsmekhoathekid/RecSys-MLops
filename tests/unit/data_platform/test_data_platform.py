from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from feature_engineering.flink.candidate_pool_job import (
    candidate_updates,
)
from feature_engineering.flink.item_features_job import (
    ItemFeatureState,
)
from feature_engineering.flink.realtime_stream_job import (
    StreamQualityTracker,
    build_realtime_feature_payloads,
    build_warehouse_rows,
    normalize_event,
    parse_message,
)
from feature_engineering.flink.user_sequence_job import (
    UserSequenceState,
)
from feature_engineering.flink.user_aggregate_job import (
    UserAggregateState,
)
from feature_engineering.spark.build_bst_training_table import (
    build_bst_training_table,
)
from feature_engineering.spark.build_item_features import (
    build_item_features,
)
from feature_engineering.spark.build_ranking_labels import (
    build_ranking_labels,
)
from feature_engineering.spark.build_user_sequence_features import (
    build_user_sequence_features,
)
from preprocess.event_dedup import (
    deduplicate_behavior_events,
)
from preprocess.point_in_time import get_time_bucket
from preprocess.schema_evolution import (
    normalize_behavior_schema,
)
from ingest.bronze_cdc_reader import (
    extract_debezium_after,
    normalize_behavior_events_from_cdc,
)
from validate.feature_quality_checks import (
    check_feast_feature_table,
    check_sequence_lengths,
)
from validate.great_expectations_runner import validate_table
from validate.great_expectations_runner import STAGING_TABLE_CONTRACTS
from validate.offline_feature_drift import (
    analyze_feature_view,
    pushgateway_samples,
    split_reference_current,
)
from monitoring.pushgateway import render_samples
from mlops.trigger_kubeflow_retrain import failed_features, trigger_retrain
from feature_store.online_writer import dumps_feature_payload
from feature_store.offline_to_online_sync import (
    latest_by_entity,
    row_payload,
    should_overwrite,
    sync_offline_to_online,
)
from feature_store.feast_registry import (
    apply_and_materialize_incremental,
    backup_registry,
    registry_path,
    restore_registry_backup,
)
from warehouse.historical_loader import normalize_staging_frame
from warehouse.schemas import STAGING_STREAM_BEHAVIOR_EVENTS
from warehouse.writer import _normalize_value, create_table_sql, upsert_sql


def _ts(value: str) -> pd.Timestamp:
    return pd.Timestamp(value, tz="UTC")


def test_deduplicate_behavior_events_keeps_latest_conflict():
    events = pd.DataFrame(
        [
            {"event_id": "e1", "payload_hash": "a", "ingestion_ts": _ts("2026-01-01T00:01:00")},
            {"event_id": "e1", "payload_hash": "b", "ingestion_ts": _ts("2026-01-01T00:02:00")},
            {"event_id": "e2", "payload_hash": "c", "ingestion_ts": _ts("2026-01-01T00:03:00")},
            {"event_id": "e2", "payload_hash": "c", "ingestion_ts": _ts("2026-01-01T00:04:00")},
        ]
    )
    result = deduplicate_behavior_events(events)
    assert len(result.clean) == 2
    assert result.conflicting_duplicate_count == 1
    assert result.exact_duplicate_count == 1
    assert result.clean.loc[result.clean["event_id"] == "e1", "payload_hash"].iloc[0] == "b"


def test_schema_evolution_defaults_missing_optional_fields():
    normalized = normalize_behavior_schema(pd.DataFrame([{"event_id": "e1"}]))
    assert normalized["device_type"].iloc[0] == "unknown"
    assert normalized["campaign_id"].iloc[0] == "none"


def test_time_bucket_matches_design():
    prediction = datetime(2026, 1, 1, 1, 0, tzinfo=timezone.utc)
    assert get_time_bucket(prediction, datetime(2026, 1, 1, 0, 58, tzinfo=timezone.utc)) == 1
    assert get_time_bucket(prediction, datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)) == 3
    assert get_time_bucket(prediction, datetime(2026, 1, 2, 0, 0, tzinfo=timezone.utc)) == 0


def test_user_sequence_feature_contract_and_length():
    events = pd.DataFrame(
        [
            {
                "user_id": 1,
                "product_id": 10 + index,
                "category_id": 2,
                "brand_id": 3,
                "price_bucket": 4,
                "event_type": "view",
                "event_type_id": 1,
                "event_timestamp": _ts(f"2026-01-01T00:0{index}:00"),
                "request_id": f"r{index}",
                "impression_id": f"i{index}",
            }
            for index in range(3)
        ]
    )
    features = build_user_sequence_features(events, max_history_length=2)
    assert features["hist_length"].max() == 2
    assert features.iloc[-1]["hist_item_ids"] == [11, 12]
    assert features.iloc[-1]["hist_event_timestamps"] == [
        "2026-01-01T00:01:00+00:00",
        "2026-01-01T00:02:00+00:00",
    ]
    assert check_sequence_lengths(features, 2).passed


def test_ranking_labels_and_bst_training_are_point_in_time():
    impressions = pd.DataFrame(
        [
            {
                "impression_id": "i1",
                "request_id": "r1",
                "user_id": 1,
                "candidate_product_id": 10,
                "impression_timestamp": _ts("2026-01-01T00:02:00"),
                "rank_position": 1,
                "candidate_source": "popular",
            }
        ]
    )
    events = pd.DataFrame(
        [
            {
                "user_id": 1,
                "product_id": 9,
                "category_id": 2,
                "brand_id": 3,
                "price_bucket": 4,
                "event_type": "view",
                "event_type_id": 1,
                "event_timestamp": _ts("2026-01-01T00:01:00"),
                "request_id": "r0",
                "impression_id": "i0",
            },
            {
                "user_id": 1,
                "product_id": 10,
                "category_id": 2,
                "brand_id": 3,
                "price_bucket": 4,
                "event_type": "cart",
                "event_type_id": 2,
                "event_timestamp": _ts("2026-01-01T00:03:00"),
                "request_id": "r1",
                "impression_id": "i1",
            },
        ]
    )
    labels = build_ranking_labels(impressions, events)
    sequence = build_user_sequence_features(events)
    item = build_item_features(
        events,
        pd.DataFrame(
            [
                {
                    "product_id": 10,
                    "category_id": 2,
                    "brand_id": 3,
                    "price_bucket": 4,
                    "is_active": True,
                }
            ]
        ),
    )
    aggregate = pd.DataFrame(
        [
            {
                "user_id": 1,
                "feature_timestamp": _ts("2026-01-01T00:01:00"),
                "views_30m": 1,
                "carts_30m": 0,
                "purchases_24h": 0,
            }
        ]
    )
    training = build_bst_training_table(labels, sequence, aggregate, item)
    assert labels["label"].iloc[0] == 1
    assert training["hist_item_id"].iloc[0] == [9]


def test_item_features_use_latest_product_metadata_for_scd_rows():
    events = pd.DataFrame(
        [
            {
                "user_id": 1,
                "product_id": 10,
                "category_id": 2,
                "brand_id": 3,
                "price_bucket": 4,
                "event_type": "view",
                "event_type_id": 1,
                "event_timestamp": _ts("2026-01-01T00:01:00"),
            }
        ]
    )
    products = pd.DataFrame(
        [
            {
                "product_id": 10,
                "valid_from": _ts("2025-01-01T00:00:00"),
                "category_id": 2,
                "brand_id": 3,
                "price_bucket": 4,
                "is_active": True,
            },
            {
                "product_id": 10,
                "valid_from": _ts("2026-01-01T00:00:00"),
                "category_id": 5,
                "brand_id": 6,
                "price_bucket": 7,
                "is_active": True,
            },
        ]
    )
    item = build_item_features(events, products)
    assert item["category_id"].iloc[0] == 5
    assert item["brand_id"].iloc[0] == 6
    assert item["price_bucket"].iloc[0] == 7


def test_feast_feature_table_required_columns():
    frame = pd.DataFrame(
        [
            {
                "user_id": 1,
                "event_timestamp": _ts("2026-01-01T00:00:00"),
                "views_30m": 1,
            }
        ]
    )
    assert check_feast_feature_table(frame, ["user_id"], ["views_30m"], "user_aggregate").passed


def test_streaming_payloads_and_candidate_updates():
    event = {
        "user_id": 1,
        "product_id": 10,
        "category_id": 2,
        "brand_id": 3,
        "price_bucket": 4,
        "price": 9.99,
        "event_type": "view",
        "event_timestamp": "2026-01-01T00:00:00Z",
    }
    sequence_payload = UserSequenceState(max_history_length=2).update(event)
    item_payload = ItemFeatureState().update(event)
    updates = candidate_updates(item_payload)
    assert sequence_payload["sequence_length"] == 1
    assert item_payload["views_1h"] == 1
    assert ("candidate:trending:1h", 10, 1.0) in updates


def test_user_aggregate_defaults_avg_viewed_price_without_views():
    payload = UserAggregateState().update(
        {
            "user_id": 1,
            "product_id": 10,
            "category_id": 2,
            "brand_id": 3,
            "price_bucket": 4,
            "price": 9.99,
            "event_type": "cart",
            "event_timestamp": "2026-01-01T00:00:00Z",
        }
    )
    assert payload["avg_viewed_price_7d"] == 0.0


def test_debezium_after_extraction_skips_deletes():
    assert extract_debezium_after({"payload": {"op": "d", "after": {"event_id": "e1"}}}) is None
    after = extract_debezium_after({"payload": {"op": "c", "after": {"event_id": "e2"}}})
    assert after == {"event_id": "e2"}
    assert parse_message(b'{"payload":{"op":"c","after":{"event_id":"e3"}}}') == {"event_id": "e3"}


def test_bronze_behavior_normalization_adds_event_type_id():
    frame = pd.DataFrame(
        [
            {
                "event_id": "e1",
                "event_type": "purchase",
                "event_timestamp": "2026-01-01T00:00:00Z",
            }
        ]
    )
    normalized = normalize_behavior_events_from_cdc(frame)
    assert normalized["event_type_id"].iloc[0] == 3
    assert str(normalized["event_timestamp"].dt.tz) == "UTC"


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


def test_warehouse_upsert_sql_targets_staging_contract():
    ddl = create_table_sql(STAGING_STREAM_BEHAVIOR_EVENTS)
    sql = upsert_sql(STAGING_STREAM_BEHAVIOR_EVENTS, list(STAGING_STREAM_BEHAVIOR_EVENTS.columns))
    assert 'CREATE TABLE IF NOT EXISTS "staging"."stream_behavior_events"' in ddl
    assert 'ON CONFLICT ("event_id") DO UPDATE SET' in sql
    assert '"late_by_seconds" = EXCLUDED."late_by_seconds"' in sql


def test_historical_staging_normalizes_nullable_user_preference_pk():
    frame = pd.DataFrame(
        [
            {
                "user_id": 1,
                "category_id": 2,
                "brand_id": None,
                "preference_weight": 0.5,
            }
        ]
    )
    normalized = normalize_staging_frame("user_preferences", frame)
    assert normalized["brand_id"].iloc[0] == 0


def test_json_payload_serializers_replace_nan_with_null():
    payload = {"avg_viewed_price_7d": float("nan"), "history": [1, float("inf")]}
    assert _normalize_value(payload) == '{"avg_viewed_price_7d": null, "history": [1, null]}'
    assert dumps_feature_payload(payload) == '{"avg_viewed_price_7d": null, "history": [1, null]}'


def test_flink_builds_warehouse_rows_from_same_payloads_as_redis():
    event = normalize_event(
        {
            "event_id": "e1",
            "user_id": "1",
            "product_id": "10",
            "event_type": "view",
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
    rows = build_warehouse_rows(event, sequence, aggregate, item, "cdc.behavior_events", 60)
    assert rows["stream_behavior_events"][0]["event_id"] == "e1"
    assert rows["stream_user_sequence_features"][0]["sequence_length"] == 1
    assert rows["stream_item_features"][0]["views_1h"] == 1


def test_stream_quality_tracker_marks_bursty_and_late_windows():
    tracker = StreamQualityTracker("cdc.behavior_events", window_seconds=60, burst_threshold_event_count=2)
    assert tracker.update("2026-01-01T00:00:01Z", 10.0, False) == []
    assert tracker.update("2026-01-01T00:00:02Z", 120.0, True) == []
    flushed = tracker.flush()
    assert len(flushed) == 1
    assert flushed[0].is_bursty is True
    assert flushed[0].late_event_count == 1


def test_great_expectations_runner_catches_duplicate_and_skew():
    frame = pd.DataFrame(
        [
            {"event_id": "e1", "event_timestamp": _ts("2026-01-01T00:00:00"), "user_id": 1, "product_id": 10, "event_type": "view", "category_id": 1},
            {"event_id": "e1", "event_timestamp": _ts("2026-01-01T00:01:00"), "user_id": 2, "product_id": 11, "event_type": "view", "category_id": 1},
        ]
    )
    result = validate_table(
        "staging.stream_behavior_events",
        frame,
        required_columns=["event_id", "event_timestamp", "user_id", "product_id", "event_type"],
        unique_columns=["event_id"],
        categorical_columns=["event_type", "category_id"],
        freshness_column="event_timestamp",
        max_top_value_ratio=0.5,
        max_unique_ratio=1.0,
    )
    assert not result.passed
    assert result.metrics["duplicate_count"] == 1
    assert any("skewed" in error for error in result.errors)


def test_great_expectations_does_not_treat_feature_version_as_skew_dimension():
    assert STAGING_TABLE_CONTRACTS["staging.stream_user_sequence_features"]["categorical_columns"] == []
    assert STAGING_TABLE_CONTRACTS["staging.stream_user_aggregate_features"]["categorical_columns"] == []


class FakeRedis:
    def __init__(self):
        self.values = {}
        self.ttls = {}

    def get(self, key):
        return self.values.get(key)

    def set(self, key, value, ex=None):
        self.values[key] = value
        self.ttls[key] = ex


def test_offline_sync_uses_latest_entity_rows_and_serving_keys(monkeypatch, tmp_path):
    frames = {
        "user_sequence_features": pd.DataFrame(
            [
                {"user_id": 1, "event_timestamp": _ts("2026-01-01T00:00:00"), "hist_length": 1},
                {"user_id": 1, "event_timestamp": _ts("2026-01-01T00:05:00"), "hist_length": 2},
            ]
        ),
        "user_aggregate_features": pd.DataFrame(
            [{"user_id": 1, "event_timestamp": _ts("2026-01-01T00:05:00"), "views_30m": 3}]
        ),
        "item_features": pd.DataFrame(
            [{"product_id": 10, "event_timestamp": _ts("2026-01-01T00:05:00"), "views_1h": 4}]
        ),
    }

    def fake_read(path):
        return frames[path.rstrip("/").split("/")[-1]]

    monkeypatch.setattr("feature_store.offline_to_online_sync.read_feature_table", fake_read)
    redis = FakeRedis()
    result = sync_offline_to_online("memory://offline", redis, run_id=None, write_monitoring=False)
    assert result["user_sequence_features"]["scanned_rows"] == 2
    assert result["user_sequence_features"]["synced_rows"] == 1
    assert "fs:user_sequence:1" in redis.values
    assert "fs:user_aggregate:1" in redis.values
    assert "fs:item:10" in redis.values
    assert '"hist_length": 2' in redis.values["fs:user_sequence:1"]


def test_offline_sync_row_payload_serializes_array_values():
    payload = row_payload(
        pd.Series(
            {
                "user_id": 1,
                "event_timestamp": _ts("2026-01-01T00:00:00"),
                "hist_item_ids": np.array([10, 11]),
                "optional": np.nan,
            }
        )
    )
    assert payload["hist_item_ids"] == [10, 11]
    assert payload["optional"] is None


def test_offline_sync_conflict_policy_skips_newer_redis_payload():
    existing = {"event_timestamp": "2026-01-01T00:10:00Z"}
    incoming = {"event_timestamp": "2026-01-01T00:05:00Z"}
    assert should_overwrite(existing, incoming, "event_timestamp") is False
    assert should_overwrite(existing, {"event_timestamp": "2026-01-01T00:15:00Z"}, "event_timestamp") is True
    assert should_overwrite({}, incoming, "event_timestamp") is True


def test_latest_by_entity_keeps_latest_timestamp():
    frame = pd.DataFrame(
        [
            {"user_id": 1, "event_timestamp": _ts("2026-01-01T00:00:00"), "value": 1},
            {"user_id": 1, "event_timestamp": _ts("2026-01-01T00:02:00"), "value": 2},
            {"user_id": 2, "event_timestamp": _ts("2026-01-01T00:01:00"), "value": 3},
        ]
    )
    latest = latest_by_entity(frame, "user_id", "event_timestamp")
    assert latest.sort_values("user_id")["value"].tolist() == [2, 3]


def test_feast_registry_backup_round_trips_via_s3_client(monkeypatch, tmp_path):
    repo = tmp_path / "feature_repo"
    repo.mkdir()
    (repo / "feature_store.yaml").write_text("project: test\n", encoding="utf-8")

    class FakeS3:
        uploaded: str | None = None

        def download_file(self, bucket, key, target):
            assert (bucket, key) == ("bucket", "registry.db")
            Path(target).write_text("restored-registry", encoding="utf-8")

        def upload_file(self, source, bucket, key):
            assert (bucket, key) == ("bucket", "registry.db")
            self.uploaded = Path(source).read_text(encoding="utf-8")

    fake_s3 = FakeS3()
    monkeypatch.setattr("feature_store.feast_registry._s3_client", lambda: fake_s3)

    assert restore_registry_backup(repo, "s3://bucket/registry.db") is True
    assert registry_path(repo).read_text(encoding="utf-8") == "restored-registry"
    assert backup_registry(repo, "s3://bucket/registry.db") is True
    assert fake_s3.uploaded == "restored-registry"


def test_feast_apply_and_materialize_incremental_preserves_registry_checkpoint(monkeypatch, tmp_path):
    repo = tmp_path / "feature_repo"
    repo.mkdir()
    (repo / "feature_store.yaml").write_text("project: test\n", encoding="utf-8")
    calls = []

    class Result:
        def __init__(self, stdout: str):
            self.stdout = stdout
            self.stderr = ""

    def fake_apply(path):
        calls.append(("apply", Path(path)))
        return Result("applied")

    def fake_materialize(end_ts, path):
        calls.append(("materialize-incremental", end_ts, Path(path)))
        registry_path(path).parent.mkdir(parents=True, exist_ok=True)
        registry_path(path).write_text("updated-registry", encoding="utf-8")
        return Result("materialized")

    monkeypatch.setattr("feature_store.feast_registry.restore_registry_backup", lambda *args: True)
    monkeypatch.setattr("feature_store.feast_registry.apply_feature_repo", fake_apply)
    monkeypatch.setattr("feature_store.feast_registry.materialize_incremental", fake_materialize)
    monkeypatch.setattr("feature_store.feast_registry.backup_registry", lambda *args: True)

    result = apply_and_materialize_incremental("2026-01-01T00:00:00Z", repo)
    assert calls == [
        ("apply", repo),
        ("materialize-incremental", "2026-01-01T00:00:00Z", repo),
    ]
    assert result["registry_restored"] is True
    assert result["registry_backed_up"] is True
    assert result["apply_stdout"] == "applied"
    assert result["materialize_stdout"] == "materialized"


def test_offline_feature_drift_splits_windows_and_builds_pushgateway_metrics():
    frame = pd.DataFrame(
        [
            {"event_timestamp": _ts("2026-01-01T00:00:00"), "views_30m": 1.0, "user_id": 1},
            {"event_timestamp": _ts("2026-01-02T00:00:00"), "views_30m": 2.0, "user_id": 2},
            {"event_timestamp": _ts("2026-01-10T00:00:00"), "views_30m": 10.0, "user_id": 3},
            {"event_timestamp": _ts("2026-01-11T00:00:00"), "views_30m": 11.0, "user_id": 4},
        ]
    )
    reference, current = split_reference_current(frame, current_days=7)
    results = analyze_feature_view(frame, "user_aggregate_features", threshold=0.15, current_days=7)
    payload = render_samples(pushgateway_samples("run-1", results))

    assert len(reference) == 2
    assert len(current) == 2
    assert results[0].feature == "views_30m"
    assert "recsys_ml_feature_drift_psi" in payload
    assert 'feature_view="user_aggregate_features"' in payload


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
                "features": [{"feature_view": "item_features", "feature": "views_1h", "passed": False}],
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
            return Run()

    monkeypatch.setitem(__import__("sys").modules, "kfp", type("Kfp", (), {"Client": Client}))
    result = trigger_retrain(str(report), "http://kfp", "exp", "pipeline.yaml", pushgateway_url=None)

    assert failed_features(json.loads(report.read_text(encoding="utf-8"))) == ["item_features.views_1h"]
    assert result.triggered is True
    assert result.kfp_run_id == "run-kfp-1"
