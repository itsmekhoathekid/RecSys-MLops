from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd

from pipelines.data_pipeline.feature_engineering.flink.candidate_pool_job import (
    candidate_updates,
)
from pipelines.data_pipeline.feature_engineering.flink.item_features_job import (
    ItemFeatureState,
)
from pipelines.data_pipeline.feature_engineering.flink.realtime_stream_job import (
    normalize_event,
    parse_message,
)
from pipelines.data_pipeline.feature_engineering.flink.user_sequence_job import (
    UserSequenceState,
)
from pipelines.data_pipeline.feature_engineering.spark.build_bst_training_table import (
    build_bst_training_table,
)
from pipelines.data_pipeline.feature_engineering.spark.build_item_features import (
    build_item_features,
)
from pipelines.data_pipeline.feature_engineering.spark.build_ranking_labels import (
    build_ranking_labels,
)
from pipelines.data_pipeline.feature_engineering.spark.build_user_sequence_features import (
    build_user_sequence_features,
)
from pipelines.data_pipeline.preprocess.event_dedup import (
    deduplicate_behavior_events,
)
from pipelines.data_pipeline.preprocess.point_in_time import get_time_bucket
from pipelines.data_pipeline.preprocess.schema_evolution import (
    normalize_behavior_schema,
)
from pipelines.data_pipeline.ingest.bronze_cdc_reader import (
    extract_debezium_after,
    normalize_behavior_events_from_cdc,
)
from pipelines.data_pipeline.validate.feature_quality_checks import (
    check_feast_feature_table,
    check_sequence_lengths,
)


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
