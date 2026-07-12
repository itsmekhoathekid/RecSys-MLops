from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

import feature_api
import inference_api
from ab_testing import TritonABRouter
from api_schemas import OnlineFeaturesResponse, RecommendationRequest
from shadow import ShadowRunner


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
    monkeypatch.setattr(inference_api, "_shadow_runner", None)


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


def test_inference_api_returns_control_while_shadow_candidate_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    class CountingRanker:
        def __init__(self, scores):
            self.scores = scores
            self.calls = 0

        def score(self, payload):
            self.calls += 1
            return payload["candidate_item_id"].tolist(), self.scores

    control = CountingRanker([0.1, 0.9, 0.3])
    candidate = CountingRanker([0.8, 0.2, 0.4])
    router = TritonABRouter(
        control_ranker=control,
        control_model_version="stable-v1",
        candidate_ranker=candidate,
        candidate_model_version="candidate-v2",
        shadow_enabled=True,
        shadow_sample_percent=100,
        experiment_id="shadow-split",
    )
    runner = ShadowRunner(timeout_seconds=1, max_pending=4, max_concurrency=1)
    monkeypatch.setattr(inference_api, "feature_service_client", lambda: DeterministicFeatureService())
    monkeypatch.setattr(inference_api, "ranker", lambda: router)
    monkeypatch.setattr(inference_api, "shadow_runner", lambda: runner)

    async def exercise():
        response = await inference_api.recommendations(
            RecommendationRequest(user_id=42, candidate_item_ids=[101, 102, 103], top_k=2)
        )
        await runner.drain()
        return response

    response = asyncio.run(exercise())

    assert response.model_version == "stable-v1"
    assert response.ab_variant == "control"
    assert [item.item_id for item in response.items] == [102, 103]
    assert control.calls == 1
    assert candidate.calls == 1
