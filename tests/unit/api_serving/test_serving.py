from __future__ import annotations

import numpy as np

from serving import (
    RecommendationRequest,
    build_triton_payload,
    format_top_k,
    get_online_features,
    recommend,
)


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
