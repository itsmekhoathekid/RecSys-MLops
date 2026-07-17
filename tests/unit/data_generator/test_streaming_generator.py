import random
from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from config import load_stream_config
from streaming.config import StreamGeneratorConfig
from streaming.event_factory import StreamEventFactory
from streaming.problem_pipeline import StreamProblemPipeline
from streaming.problems import (
    BurstTrafficProblem,
    DuplicateReplayProblem,
)


def test_offline_and_stream_problems_are_loaded_from_one_yaml():
    config = load_stream_config("configs/local/data_generator_test.yaml")
    assert config.problems.burst_traffic.every_n_ticks == 5
    assert config.problems.late_arrival.delay_minutes_min == 45
    assert config.problems.duplicate_replay.rate == 0.14


def test_problem_folders_match_the_rubric_scope():
    offline = {
        path.name
        for path in Path(
            "apps/data-platform/data-generator/src/offline/problems"
        ).glob("*.py")
        if path.name != "__init__.py"
    }
    streaming = {
        path.name
        for path in Path(
            "apps/data-platform/data-generator/src/streaming/problems"
        ).glob("*.py")
        if path.name != "__init__.py"
    }
    assert offline == {
        "skew.py",
        "high_cardinality.py",
        "schema_evolution.py",
        "exact_duplicate.py",
    }
    assert streaming == {
        "burst_traffic.py",
        "late_arrival.py",
        "duplicate_replay.py",
    }


def test_stream_problem_settings_are_not_owned_by_helm():
    values = Path("infra/helm/recsys-data-platform/values.yaml").read_text()
    configmap = Path(
        "infra/helm/recsys-data-platform/templates/configmap.yaml"
    ).read_text()
    deployment = Path(
        "infra/helm/recsys-data-platform/templates/realtime-producer.yaml"
    ).read_text()

    assert "eventsPerTick" not in values
    assert "REALTIME_DUPLICATE_EVENT_RATE" not in configmap
    assert '--config "$DATA_GENERATOR_CONFIG"' in deployment


def test_burst_is_an_independent_problem_class():
    assert BurstTrafficProblem(5, 8).events_for_tick(5, 40) == 320


def test_event_factory_preserves_relational_source_contract():
    now = datetime(2026, 7, 17, tzinfo=timezone.utc)
    rows = StreamEventFactory(80, 160).create(2, now, now)
    assert set(rows) == {
        "sessions",
        "recommendation_requests",
        "impressions",
        "behavior_events",
        "orders",
        "order_items",
    }
    assert (
        rows["behavior_events"]["request_id"]
        == rows["recommendation_requests"]["request_id"]
    )
    assert (
        rows["behavior_events"]["impression_id"]
        == rows["impressions"]["impression_id"]
    )


def test_late_arrival_is_the_only_stream_timing_problem():
    config = StreamGeneratorConfig.model_validate(
        {
            "problems": {
                "late_arrival": {
                    "rate": 1.0,
                    "delay_minutes_min": 10,
                    "delay_minutes_max": 10,
                },
            }
        }
    )
    now = datetime(2026, 7, 17, tzinfo=timezone.utc)
    result = StreamProblemPipeline(random.Random(42), config.problems).event_time(now)
    assert result.late is True
    assert result.delay_seconds == 600


def test_removed_stream_problem_keys_are_rejected():
    with pytest.raises(ValidationError):
        StreamGeneratorConfig.model_validate(
            {"problems": {"out_of_order": {"rate": 1.0}}}
        )
    with pytest.raises(ValidationError):
        StreamGeneratorConfig.model_validate(
            {
                "problems": {
                    "duplicate_replay": {
                        "rate": 0.10,
                        "conflicting_rate": 0.50,
                    }
                }
            }
        )


def test_duplicate_replay_preserves_identity_and_payload():
    now = datetime(2026, 7, 17, tzinfo=timezone.utc)
    original = StreamEventFactory(80, 160).create(0, now, now)
    duplicate = DuplicateReplayProblem(random.Random(2), rate=1.0, history_size=10)
    duplicate.remember(original)

    replay = duplicate.replay(now)
    assert replay is not None
    assert (
        replay["behavior_events"]["event_id"]
        == original["behavior_events"]["event_id"]
    )
    assert (
        replay["behavior_events"]["payload_hash"]
        == original["behavior_events"]["payload_hash"]
    )
