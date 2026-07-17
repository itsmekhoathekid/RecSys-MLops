from __future__ import annotations

import sys
import asyncio
import time
import types

import numpy as np
import pytest

import online_features
from ab_testing import TritonABRouter
from api_schemas import RecommendationRequest
from observability import METRICS, metrics_text, span
from online_features import (
    FeatureClient,
    get_online_features,
    normalize_realtime_user_features,
    parse_json_bytes,
)
from ranking import (
    as_int_list,
    build_triton_payload,
    embedding_index,
    format_top_k,
    normalize_item_features,
    normalize_sequence_features,
    recommend,
)
from triton import TritonRanker
from shadow import ShadowRunner


def test_build_triton_payload_maps_feature_rows_to_tensors():
    payload = build_triton_payload(
        sequence_row={
            "hist_item_ids": "[1, 2, 3]",
            "hist_event_type_ids": [1, 1, 2],
            "hist_category_ids": [4, 5, 6],
            "hist_brand_ids": [7, 8, 9],
            "hist_price_bucket_ids": [1, 2, 3],
            "hist_time_ids": [1, 2, 3],
        },
        item_rows={
            10: {"category_id": 2, "brand_id": 3, "price_bucket": 4},
            11: {"category_id": 5, "brand_id": 6, "price_bucket": 7},
        },
        candidate_item_ids=[10, 11],
    )

    assert payload["hist_item_id"].tolist() == [1, 2, 3]
    assert payload["candidate_item_id"].tolist() == [10, 11]
    assert payload["candidate_category"].tolist() == [2, 5]
    assert payload["candidate_brand"].dtype == np.int64


def test_json_and_embedding_helpers_handle_empty_and_invalid_values(monkeypatch):
    monkeypatch.setenv("MODEL_ITEM_NUM", "10")

    assert parse_json_bytes(None) == {}
    assert parse_json_bytes(b"") == {}
    assert parse_json_bytes(b'{"a": 1}') == {"a": 1}
    assert as_int_list(None) == []
    assert as_int_list("not-json") == []
    assert as_int_list("[1, null, 3]") == [1, 3]
    assert as_int_list(("4", 5)) == [4, 5]
    assert embedding_index(27, "item") == 7
    assert normalize_item_features(27, None).item_id == 7

    sequence = normalize_sequence_features(
        {
            "hist_item_ids": "[11, 12]",
            "hist_event_type_ids": [1],
            "hist_category_ids": [2],
            "hist_brand_ids": [3],
            "hist_price_bucket_ids": [4],
            "hist_time_ids": [5],
        }
    )
    assert sequence["hist_item_id"] == [1, 2]


def test_realtime_flink_sequence_is_mapped_to_feature_api_schema():
    payload = normalize_realtime_user_features(
        {
            "item_ids": [15],
            "event_type_ids": [1],
            "category_ids": [6],
            "brand_ids": [3],
            "price_bucket_ids": [2],
            "event_timestamps": ["2026-07-13T06:15:26Z"],
            "request_ids": ["web-event-123"],
            "impression_ids": [""],
            "sequence_length": 1,
            "max_history_length": 50,
            "feature_version": "bst_sequence_v2",
        },
        {"views_30m": 1, "carts_30m": 0, "purchases_24h": 0},
    )

    assert payload["hist_item_ids"] == [15]
    assert payload["hist_event_type_ids"] == [1]
    assert payload["hist_request_ids"] == ["web-event-123"]
    assert payload["hist_length"] == 1
    assert payload["views_30m"] == 1


def test_build_triton_payload_maps_raw_online_ids_to_embedding_space(monkeypatch):
    monkeypatch.setenv("MODEL_ITEM_NUM", "100")
    monkeypatch.setenv("MODEL_CATEGORY_NUM", "30")
    monkeypatch.setenv("MODEL_BRAND_NUM", "740")
    monkeypatch.setenv("MODEL_PRICE_BUCKET_NUM", "10")
    monkeypatch.setenv("MODEL_EVENT_TYPE_NUM", "3")

    payload = build_triton_payload(
        sequence_row={
            "hist_item_ids": [800046],
            "hist_event_type_ids": [3],
            "hist_category_ids": [9001],
            "hist_brand_ids": [8004],
            "hist_price_bucket_ids": [16],
        },
        item_rows={
            800038: {"category_id": 9003, "brand_id": 8003, "price_bucket": 18},
        },
        candidate_item_ids=[800038],
    )

    assert payload["hist_item_id"].tolist() == [46]
    assert payload["hist_event_type"].tolist() == [0]
    assert payload["hist_category"].tolist() == [1]
    assert payload["hist_brand"].tolist() == [604]
    assert payload["hist_price_bucket"].tolist() == [6]
    assert payload["candidate_item_id"].tolist() == [38]
    assert payload["candidate_category"].tolist() == [3]
    assert payload["candidate_brand"].tolist() == [603]
    assert payload["candidate_price_bucket"].tolist() == [8]


def test_format_top_k_sorts_scores_descending():
    response = format_top_k(
        user_id=1,
        model_version="v1",
        candidate_item_ids=[10, 11, 12],
        scores=[0.2, 0.9, 0.5],
        top_k=2,
    )

    assert [item.item_id for item in response.items] == [11, 12]
    assert [item.score for item in response.items] == [0.9, 0.5]


def test_span_preserves_body_exceptions():
    with pytest.raises(RuntimeError, match="boom"):
        with span("unit.test.span"):
            raise RuntimeError("boom")


def test_recommend_uses_fallback_candidates_and_ranker_scores():
    class Features:
        def candidates(self, user_id, limit):
            return [10, 11]

        def user_sequence(self, user_id):
            return {"hist_item_ids": [1], "hist_event_type_ids": [1]}

        def item_features(self, item_id):
            return {"category_id": item_id, "brand_id": 1, "price_bucket": 2}

    class Ranker:
        def score(self, payload):
            return payload["candidate_item_id"].tolist(), [0.1, 0.8]

    response = recommend(
        request=RecommendationRequest(user_id=1, top_k=1),
        feature_client=Features(),
        ranker=Ranker(),
        model_version="trial-001",
    )

    assert response.model_version == "trial-001"
    assert [item.item_id for item in response.items] == [11]


def test_ab_assignment_is_deterministic_and_respects_weight():
    class Ranker:
        def score(self, payload):
            return [], []

    router = TritonABRouter(
        control_ranker=Ranker(),
        control_model_version="stable",
        candidate_ranker=Ranker(),
        candidate_model_version="candidate",
        enabled=True,
        candidate_weight_percent=10,
        experiment_id="exp-1",
    )

    assert router.assign(42) == router.assign(42)
    assignments = [router.assign(user_id) for user_id in range(1, 1001)]
    candidate_ratio = assignments.count("candidate") / len(assignments)
    assert 0.07 <= candidate_ratio <= 0.13


def test_ab_disabled_uses_control_route():
    class Ranker:
        def score(self, payload):
            return [], []

    router = TritonABRouter(
        control_ranker=Ranker(),
        control_model_version="stable",
        candidate_ranker=Ranker(),
        candidate_model_version="candidate",
        enabled=False,
        candidate_weight_percent=100,
        experiment_id="exp-1",
    )

    route = router.route(1)

    assert route.ab_variant is None
    assert route.model_version == "stable"
    text = metrics_text()
    assert "recsys_api_rollout_config_info" in text
    assert 'control_model_version="stable"' in text
    assert 'mode="stable"' in text


def test_shadow_route_keeps_user_response_on_control_and_samples_candidate():
    class Ranker:
        def score(self, payload):
            return [], []

    control = Ranker()
    candidate = Ranker()
    router = TritonABRouter(
        control_ranker=control,
        control_model_version="stable",
        candidate_ranker=candidate,
        candidate_model_version="candidate",
        shadow_enabled=True,
        shadow_sample_percent=100,
        experiment_id="shadow-exp",
    )

    response_route = router.route(42)
    shadow_route = router.shadow_route(42)

    assert response_route.ranker is control
    assert response_route.ab_variant == "control"
    assert response_route.model_version == "stable"
    assert shadow_route is not None
    assert shadow_route.ranker is candidate
    assert shadow_route.ab_variant == "shadow_candidate"
    assert shadow_route.model_version == "candidate"
    text = metrics_text()
    assert 'candidate_model_version="candidate"' in text
    assert 'experiment_id="shadow-exp"' in text
    assert 'mode="shadow"' in text

    sampled_router = TritonABRouter(
        control_ranker=control,
        control_model_version="stable",
        candidate_ranker=candidate,
        candidate_model_version="candidate",
        shadow_enabled=True,
        shadow_sample_percent=50,
        experiment_id="shadow-sample",
    )
    sampled = [sampled_router.shadow_route(user_id) for user_id in range(1, 1001)]
    sampled_ratio = sum(route is not None for route in sampled) / len(sampled)
    assert 0.45 <= sampled_ratio <= 0.55

    disabled_sample = TritonABRouter(
        control_ranker=control,
        control_model_version="stable",
        candidate_ranker=candidate,
        shadow_enabled=True,
        shadow_sample_percent=0,
    )
    assert disabled_sample.shadow_route(1) is None


def test_shadow_runner_records_success_error_timeout_and_drop():
    from ab_testing import TritonRoute

    class SuccessRanker:
        def score(self, payload):
            return [1, 2], [0.2, 0.8]

    class ErrorRanker:
        def score(self, payload):
            raise RuntimeError("candidate unavailable")

    class SlowRanker:
        def score(self, payload):
            time.sleep(0.05)
            return [1], [0.1]

    async def exercise():
        payload = {"candidate_item_id": np.asarray([1, 2], dtype=np.int64)}
        success = ShadowRunner(timeout_seconds=1, max_pending=2, max_concurrency=1)
        assert success.submit(TritonRoute(SuccessRanker(), "candidate", "shadow_candidate", "shadow-ok"), payload)
        await success.drain()

        error = ShadowRunner(timeout_seconds=1, max_pending=2, max_concurrency=1)
        assert error.submit(TritonRoute(ErrorRanker(), "candidate", "shadow_candidate", "shadow-error"), payload)
        await error.drain()

        timeout = ShadowRunner(timeout_seconds=0.001, max_pending=1, max_concurrency=1)
        route = TritonRoute(SlowRanker(), "candidate", "shadow_candidate", "shadow-timeout")
        assert timeout.submit(route, payload)
        assert timeout.submit(route, payload) is False
        await timeout.drain()

    asyncio.run(exercise())
    text = metrics_text()
    assert 'experiment_id="shadow-ok",model_version="candidate",status="success"' in text
    assert 'experiment_id="shadow-error",model_version="candidate",status="error"' in text
    assert 'experiment_id="shadow-timeout",model_version="candidate",status="timeout"' in text
    assert 'experiment_id="shadow-timeout",model_version="candidate",status="dropped"' in text
    assert "recsys_api_shadow_latency_seconds_bucket" in text
    assert "recsys_api_shadow_score_mean" in text


def test_recommend_ab_router_returns_variant_metadata_and_metrics():
    class Features:
        def candidates(self, user_id, limit):
            return [10, 11]

        def user_sequence(self, user_id):
            return {"hist_item_ids": [1], "hist_event_type_ids": [1]}

        def item_features(self, item_id):
            return {"category_id": item_id, "brand_id": 1, "price_bucket": 2}

    class Ranker:
        def score(self, payload):
            return payload["candidate_item_id"].tolist(), [0.1, 0.8]

    router = TritonABRouter(
        control_ranker=Ranker(),
        control_model_version="stable",
        candidate_ranker=Ranker(),
        candidate_model_version="candidate",
        enabled=True,
        candidate_weight_percent=100,
        experiment_id="exp-1",
    )

    response = recommend(
        request=RecommendationRequest(user_id=1, top_k=1),
        feature_client=Features(),
        ranker=router,
        model_version="stable",
    )
    text = metrics_text()

    assert response.model_version == "candidate"
    assert response.ab_variant == "candidate"
    assert response.ab_experiment_id == "exp-1"
    assert 'ab_variant="candidate"' in text
    assert 'model_version="candidate"' in text
    assert 'experiment_id="exp-1"' in text
    assert "recsys_api_score_mean" in text
    assert "model_predictions_total" in text
    assert "model_prediction_latency_seconds_bucket" in text
    assert "model_prediction_confidence_bucket" in text


def test_get_online_features_reads_candidates_sequence_and_items():
    class Features:
        def candidates(self, user_id, limit):
            return [10, 11]

        def user_sequence(self, user_id):
            return {"hist_item_ids": [1], "hist_event_type_ids": [1]}

        def item_features(self, item_id):
            return {"category_id": item_id, "brand_id": 1, "price_bucket": 2}

    response = get_online_features(
        user_id=1,
        candidate_item_ids=None,
        top_k=1,
        feature_client=Features(),
    )

    assert response.candidate_item_ids == [10, 11]
    assert response.user_sequence["hist_item_ids"] == [1]
    assert response.item_features["10"]["category_id"] == 10


def test_feature_client_returns_defaults_when_online_store_is_unavailable(monkeypatch):
    class BrokenRedis:
        def __init__(self, *args, **kwargs):
            pass

        def get(self, key):
            raise OSError("redis unavailable")

        def zrevrange(self, key, start, end):
            raise OSError("redis unavailable")

    monkeypatch.setattr(online_features, "redis", type("RedisModule", (), {"Redis": BrokenRedis}), raising=False)

    original_import = __import__

    def fake_import(name, *args, **kwargs):
        if name == "redis":
            return type("RedisModule", (), {"Redis": BrokenRedis})
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", fake_import)
    client = FeatureClient(allow_fallback=True)

    assert client.user_sequence(1) == {}
    assert client.item_features(1) == {}
    assert client.candidates(1, 3) == [1, 2, 3]


def test_feature_client_success_and_error_without_fallback(monkeypatch):
    class RedisClient:
        def zrevrange(self, key, start, end):
            return [b"10", "11"]

    monkeypatch.setattr(
        online_features,
        "redis",
        type("RedisModule", (), {"Redis": lambda *args, **kwargs: RedisClient()}),
        raising=False,
    )

    original_import = __import__

    def fake_import(name, *args, **kwargs):
        if name == "redis":
            return type("RedisModule", (), {"Redis": lambda *a, **k: RedisClient()})
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", fake_import)
    client = FeatureClient(allow_fallback=False)

    def fake_get_online_features(features, entity_rows):
        if "user_id" in entity_rows[0]:
            return {"user_id": [1], "hist_item_ids": [[1]], "hist_event_type_ids": [[1]]}
        product_id = entity_rows[0]["product_id"]
        if product_id == 10:
            return {"product_id": [10], "category_id": [2], "brand_id": [3], "price_bucket": [4]}
        raise OSError("missing")

    monkeypatch.setattr(client, "_get_feast_online_features", fake_get_online_features)

    assert client.user_sequence(1)["hist_item_ids"] == [1]
    assert client.item_features(10)["category_id"] == 2
    assert client.candidates(1, 2) == [10, 11]

    try:
        client.item_features(99)
    except RuntimeError as exc:
        assert "failed to fetch item features from Feast online store" in str(exc)
    else:
        raise AssertionError("expected item feature Redis failure")


def test_feature_client_prefers_user_candidates_and_fills_from_global():
    class RedisClient:
        def __init__(self):
            self.keys = []

        def zrevrange(self, key, start, end):
            self.keys.append(key)
            if key == "candidate:user:7":
                return [b"21", b"10"]
            if key == "candidate:popular:global":
                return [b"10", b"11", b"12"]
            return []

    client = object.__new__(FeatureClient)
    client.allow_fallback = False
    client.client = RedisClient()

    assert client.candidates(7, 4) == [21, 10, 11, 12]
    assert client.client.keys == ["candidate:user:7", "candidate:popular:global"]


def test_feature_client_falls_back_to_global_candidates_for_new_user():
    class RedisClient:
        def zrevrange(self, key, start, end):
            if key == "candidate:popular:global":
                return [b"10", b"11"]
            return []

    client = object.__new__(FeatureClient)
    client.allow_fallback = False
    client.client = RedisClient()

    assert client.candidates(99, 2) == [10, 11]


def test_triton_ranker_scores_and_records_errors(monkeypatch):
    class InferInput:
        def __init__(self, name, shape, dtype):
            self.name = name
            self.shape = shape
            self.dtype = dtype
            self.values = None

        def set_data_from_numpy(self, values):
            self.values = values

    class InferRequestedOutput:
        def __init__(self, name):
            self.name = name

    class Result:
        def as_numpy(self, name):
            if name == "candidate_item_id_out":
                return np.asarray([1, 2], dtype=np.int64)
            return np.asarray([0.2, 0.8], dtype=np.float32)

    class Client:
        should_fail = False

        def __init__(self, url):
            self.url = url

        def infer(self, model_name, inputs, outputs):
            if self.should_fail:
                raise RuntimeError("triton down")
            assert model_name == "bst_ensemble"
            assert inputs[0].dtype == "INT64"
            assert outputs[0].name == "candidate_item_id_out"
            return Result()

    grpc = types.SimpleNamespace(
        InferInput=InferInput,
        InferRequestedOutput=InferRequestedOutput,
        InferenceServerClient=Client,
    )
    monkeypatch.setitem(sys.modules, "tritonclient", types.SimpleNamespace(grpc=grpc))
    monkeypatch.setitem(sys.modules, "tritonclient.grpc", grpc)

    ranker = TritonRanker(url="localhost:9000", model_name="bst_ensemble", model_version="v1")
    item_ids, scores = ranker.score({"candidate_item_id": np.asarray([1, 2], dtype=np.int64)})
    assert item_ids == [1, 2]
    assert len(scores) == 2

    Client.should_fail = True
    try:
        ranker.score({"candidate_item_id": np.asarray([1], dtype=np.int64)})
    except RuntimeError as exc:
        assert "triton down" in str(exc)
    else:
        raise AssertionError("expected triton failure")


def test_ab_router_from_env_builds_candidate_ranker(monkeypatch):
    import ab_testing

    created = []

    class FakeRanker:
        def __init__(self, **kwargs):
            created.append(kwargs)

        def score(self, payload):
            return [], []

    monkeypatch.setattr(ab_testing, "TritonRanker", FakeRanker)
    monkeypatch.setenv("AB_TEST_ENABLED", "1")
    monkeypatch.setenv("AB_CANDIDATE_TRITON_URL", "candidate:9000")
    monkeypatch.setenv("AB_CANDIDATE_MODEL_VERSION", "candidate-v1")
    monkeypatch.setenv("AB_CANDIDATE_WEIGHT_PERCENT", "25")
    monkeypatch.setenv("AB_EXPERIMENT_ID", "exp-env")
    monkeypatch.setenv("MODEL_VERSION", "stable-v1")

    router = TritonABRouter.from_env()

    assert router.enabled is True
    assert router.candidate_weight_percent == 25
    assert router.experiment_id == "exp-env"
    assert created[0]["model_version"] == "stable-v1"
    assert created[1]["model_version"] == "candidate-v1"


def test_ab_router_from_env_builds_shadow_candidate_with_zero_user_weight(monkeypatch):
    import ab_testing

    created = []

    class FakeRanker:
        def __init__(self, **kwargs):
            created.append(kwargs)

        def score(self, payload):
            return [], []

    monkeypatch.setattr(ab_testing, "TritonRanker", FakeRanker)
    monkeypatch.setenv("AB_TEST_ENABLED", "0")
    monkeypatch.setenv("AB_SHADOW_ENABLED", "1")
    monkeypatch.setenv("AB_SHADOW_SAMPLE_PERCENT", "100")
    monkeypatch.setenv("AB_CANDIDATE_TRITON_URL", "candidate:9000")
    monkeypatch.setenv("AB_CANDIDATE_MODEL_VERSION", "candidate-v2")
    monkeypatch.setenv("AB_CANDIDATE_WEIGHT_PERCENT", "0")

    router = TritonABRouter.from_env()

    assert router.enabled is False
    assert router.shadow_enabled is True
    assert router.route(1).ab_variant == "control"
    assert router.shadow_route(1).model_version == "candidate-v2"
    assert len(created) == 2
    assert created[1]["ab_variant"] == "shadow_candidate"


def test_observability_metrics_expose_api_redis_and_triton_series():
    METRICS.inc("recsys_api_requests_total", labels={"route": "/recommendations", "method": "POST", "status": "200"})
    METRICS.inc("recsys_api_redis_errors_total", labels={"operation": "user_sequence"})
    METRICS.inc("recsys_api_triton_errors_total", labels={"model_name": "bst_ensemble"})
    text = metrics_text()

    assert "recsys_api_requests_total" in text
    assert 'route="/recommendations"' in text
    assert "recsys_api_redis_errors_total" in text
    assert "recsys_api_triton_errors_total" in text
