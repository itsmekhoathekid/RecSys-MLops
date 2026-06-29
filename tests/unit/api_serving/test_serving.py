from __future__ import annotations

import sys
import types

import numpy as np
import pytest

from serving import (
    FeatureClient,
    RecommendationRequest,
    TritonRanker,
    TritonABRouter,
    as_int_list,
    build_triton_payload,
    embedding_index,
    format_top_k,
    get_online_features,
    normalize_item_features,
    normalize_sequence_features,
    parse_json_bytes,
    recommend,
)
from observability import METRICS, metrics_text, span


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
    import serving

    class BrokenRedis:
        def __init__(self, *args, **kwargs):
            pass

        def get(self, key):
            raise OSError("redis unavailable")

        def zrevrange(self, key, start, end):
            raise OSError("redis unavailable")

    monkeypatch.setattr(serving, "redis", type("RedisModule", (), {"Redis": BrokenRedis}), raising=False)

    original_import = __import__

    def fake_import(name, *args, **kwargs):
        if name == "redis":
            return type("RedisModule", (), {"Redis": BrokenRedis})
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", fake_import)
    client = serving.FeatureClient(allow_fallback=True)

    assert client.user_sequence(1) == {}
    assert client.item_features(1) == {}
    assert client.candidates(1, 3) == [1, 2, 3]


def test_feature_client_success_and_error_without_fallback(monkeypatch):
    import serving

    class RedisClient:
        def get(self, key):
            if key == "fs:user_sequence:1":
                return b'{"hist_item_ids": [1]}'
            if key == "fs:item:10":
                return '{"category_id": 2}'
            raise OSError("missing")

        def zrevrange(self, key, start, end):
            return [b"10", "11"]

    monkeypatch.setattr(serving, "redis", type("RedisModule", (), {"Redis": lambda *args, **kwargs: RedisClient()}), raising=False)

    original_import = __import__

    def fake_import(name, *args, **kwargs):
        if name == "redis":
            return type("RedisModule", (), {"Redis": lambda *a, **k: RedisClient()})
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", fake_import)
    client = FeatureClient(allow_fallback=False)

    assert client.user_sequence(1) == {"hist_item_ids": [1]}
    assert client.item_features(10) == {"category_id": 2}
    assert client.candidates(1, 2) == [10, 11]

    try:
        client.item_features(99)
    except RuntimeError as exc:
        assert "failed to fetch item features" in str(exc)
    else:
        raise AssertionError("expected item feature Redis failure")


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


def test_observability_metrics_expose_api_redis_and_triton_series():
    METRICS.inc("recsys_api_requests_total", labels={"route": "/recommendations", "method": "POST", "status": "200"})
    METRICS.inc("recsys_api_redis_errors_total", labels={"operation": "user_sequence"})
    METRICS.inc("recsys_api_triton_errors_total", labels={"model_name": "bst_ensemble"})
    text = metrics_text()

    assert "recsys_api_requests_total" in text
    assert 'route="/recommendations"' in text
    assert "recsys_api_redis_errors_total" in text
    assert "recsys_api_triton_errors_total" in text
