from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from hypothesis import given, settings
from hypothesis import strategies as st

import main as api_main
from serving import RecommendationRequest, recommend


class DeterministicFeatureClient:
    def candidates(self, user_id: int, limit: int) -> list[int]:
        return list(range(100, 100 + limit))

    def user_sequence(self, user_id: int) -> dict[str, list[int]]:
        return {
            "hist_item_ids": [1, 2, 3],
            "hist_event_type_ids": [1, 1, 2],
            "hist_category_ids": [4, 5, 6],
            "hist_brand_ids": [7, 8, 9],
            "hist_price_bucket_ids": [1, 2, 3],
            "hist_time_ids": [1, 2, 3],
        }

    def item_features(self, item_id: int) -> dict[str, int]:
        return {
            "category_id": item_id % 30,
            "brand_id": item_id % 740,
            "price_bucket": item_id % 10,
        }


class DeterministicRanker:
    model_version = "deterministic-test"

    def score(self, payload):
        candidate_count = len(payload["candidate_item_id"])
        return payload["candidate_item_id"].tolist(), [float(index) for index in range(candidate_count)]


@pytest.fixture
def deterministic_api(monkeypatch) -> TestClient:
    feature_client = DeterministicFeatureClient()
    ranker = DeterministicRanker()
    monkeypatch.setattr(api_main, "feature_client", lambda: feature_client)
    monkeypatch.setattr(api_main, "ranker", lambda: ranker)
    monkeypatch.setenv("MODEL_VERSION", ranker.model_version)
    return TestClient(api_main.app)


@pytest.mark.parametrize(
    ("payload", "expected_count"),
    [
        ({"user_id": 42, "candidate_item_ids": [101, 102, 103], "top_k": 2}, 2),
        ({"user_id": 42, "top_k": 3}, 3),
        ({"user_id": 1, "candidate_item_ids": [1], "top_k": 1}, 1),
        ({"user_id": 1, "candidate_item_ids": list(range(1, 501)), "top_k": 100}, 100),
    ],
    ids=[
        "equivalence-valid-explicit-candidates",
        "equivalence-valid-fallback-candidates",
        "boundary-min-user-top-k-and-one-candidate",
        "boundary-max-top-k-and-max-candidates",
    ],
)
def test_recommendations_web_api_equivalence_and_boundary_valid_cases(
    deterministic_api: TestClient,
    payload: dict,
    expected_count: int,
) -> None:
    response = deterministic_api.post("/recommendations", json=payload)

    assert response.status_code == 200
    body = response.json()
    assert body["model_version"] == "deterministic-test"
    assert len(body["items"]) == expected_count


@pytest.mark.parametrize(
    "payload",
    [
        {"user_id": 0, "candidate_item_ids": [1], "top_k": 1},
        {"user_id": 1, "candidate_item_ids": [1], "top_k": 0},
        {"user_id": 1, "candidate_item_ids": [1], "top_k": 101},
        {"user_id": 1, "candidate_item_ids": [], "top_k": 1},
        {"user_id": 1, "candidate_item_ids": list(range(1, 502)), "top_k": 1},
    ],
    ids=[
        "boundary-invalid-user-id-zero",
        "boundary-invalid-top-k-zero",
        "boundary-invalid-top-k-above-max",
        "boundary-invalid-empty-candidates",
        "boundary-invalid-candidates-above-max",
    ],
)
def test_recommendations_web_api_equivalence_and_boundary_invalid_cases(
    deterministic_api: TestClient,
    payload: dict,
) -> None:
    response = deterministic_api.post("/recommendations", json=payload)

    assert response.status_code == 422


@given(
    user_id=st.integers(min_value=1, max_value=20_000),
    top_k=st.integers(min_value=1, max_value=100),
    candidate_item_ids=st.lists(
        st.integers(min_value=1, max_value=20_000),
        min_size=1,
        max_size=100,
    ),
)
@settings(max_examples=60, deadline=None)
def test_property_based_recommendation_idempotency_for_deterministic_prediction(
    user_id: int,
    top_k: int,
    candidate_item_ids: list[int],
) -> None:
    request = RecommendationRequest(
        user_id=user_id,
        candidate_item_ids=candidate_item_ids,
        top_k=top_k,
    )
    feature_client = DeterministicFeatureClient()
    ranker = DeterministicRanker()

    responses = [
        recommend(
            request=request,
            feature_client=feature_client,
            ranker=ranker,
            model_version=ranker.model_version,
        ).model_dump()
        for _ in range(3)
    ]

    assert responses[0] == responses[1] == responses[2]
