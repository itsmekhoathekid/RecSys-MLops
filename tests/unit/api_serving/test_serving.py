from __future__ import annotations

import numpy as np

from serving import (
    RecommendationRequest,
    TritonABRouter,
    build_triton_payload,
    format_top_k,
    get_online_features,
    recommend,
)
from observability import METRICS, metrics_text


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


def test_observability_metrics_expose_api_redis_and_triton_series():
    METRICS.inc("recsys_api_requests_total", labels={"route": "/recommendations", "method": "POST", "status": "200"})
    METRICS.inc("recsys_api_redis_errors_total", labels={"operation": "user_sequence"})
    METRICS.inc("recsys_api_triton_errors_total", labels={"model_name": "bst_ensemble"})
    text = metrics_text()

    assert "recsys_api_requests_total" in text
    assert 'route="/recommendations"' in text
    assert "recsys_api_redis_errors_total" in text
    assert "recsys_api_triton_errors_total" in text
