from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import feature_api
import inference_api
from api_schemas import OnlineFeaturesResponse


class DeterministicFeatureClient:
    def candidates(self, user_id: int, limit: int) -> list[int]:
        return [101, 102, 103][:limit]

    def user_sequence(self, user_id: int) -> dict[str, list[int]]:
        return {"hist_item_ids": [1, 2], "hist_event_type_ids": [1, 2]}

    def item_features(self, item_id: int) -> dict[str, int]:
        return {"category_id": item_id % 30, "brand_id": item_id % 740, "price_bucket": item_id % 10}


class DeterministicRanker:
    def score(self, payload):
        return payload["candidate_item_id"].tolist(), [0.1, 0.9, 0.3]


class DeterministicRouter:
    def route(self, user_id: int):
        from ab_testing import TritonRoute

        return TritonRoute(
            ranker=DeterministicRanker(),
            model_version="split-test",
            ab_variant="control",
            ab_experiment_id="split-exp",
        )


class DeterministicFeatureService:
    async def fetch(self, request):
        candidates = request.candidate_item_ids or [101, 102, 103]
        return OnlineFeaturesResponse(
            user_id=request.user_id,
            candidate_item_ids=candidates,
            user_sequence={"hist_item_ids": [1, 2], "hist_event_type_ids": [1, 2]},
            item_features={
                str(item_id): {"category_id": item_id % 30, "brand_id": item_id % 740, "price_bucket": item_id % 10}
                for item_id in candidates
            },
        )


@pytest.fixture(autouse=True)
def reset_singletons(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(feature_api, "_feature_client", None)
    monkeypatch.setattr(inference_api, "_feature_service_client", None)
    monkeypatch.setattr(inference_api, "_ranker", None)


def test_feature_api_exposes_online_features_with_pydantic_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(feature_api, "feature_client", lambda: DeterministicFeatureClient())
    client = TestClient(feature_api.app)

    health = client.get("/healthz")
    assert health.status_code == 200
    invalid = client.post("/online-features", json={"user_id": 0, "top_k": 1})
    assert invalid.status_code == 422

    response = client.post(
        "/online-features",
        json={"user_id": 42, "candidate_item_ids": [101, 102], "top_k": 2},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["user_id"] == 42
    assert body["candidate_item_ids"] == [101, 102]
    assert body["item_features"]["101"]["category_id"] == 11


def test_inference_api_calls_feature_service_then_ranks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(inference_api, "feature_service_client", lambda: DeterministicFeatureService())
    monkeypatch.setattr(inference_api, "ranker", lambda: DeterministicRouter())
    client = TestClient(inference_api.app)

    response = client.post("/recommendations", json={"user_id": 42, "candidate_item_ids": [101, 102, 103], "top_k": 2})

    assert response.status_code == 200
    body = response.json()
    assert body["model_version"] == "split-test"
    assert body["ab_variant"] == "control"
    assert [item["item_id"] for item in body["items"]] == [102, 103]
