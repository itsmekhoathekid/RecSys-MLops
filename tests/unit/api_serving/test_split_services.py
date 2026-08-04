from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from recsys_online_feature_api.app import create_app as create_feature_app
from recsys_online_feature_api.settings import FeatureApiSettings
from recsys_inference_api.ab_testing import TritonABRouter, TritonRoute
from recsys_inference_api.app import create_app as create_inference_app
from recsys_inference_api.settings import InferenceApiSettings
from recsys_inference_api.shadow import ShadowRunner
from recsys_serving_common.contracts import OnlineFeaturesResponse


class DeterministicFeatureClient:
    def _feature_store(self):
        return "store"

    def close(self):
        pass

    def candidates(self, user_id: int, limit: int) -> list[int]:
        return [101, 102, 103][:limit]

    def user_sequence(self, user_id: int) -> dict[str, list[int]]:
        return {"hist_item_ids": [1, 2], "hist_event_type_ids": [1, 2]}

    def item_features(self, item_id: int) -> dict[str, int]:
        return {
            "category_id": item_id % 30,
            "brand_id": item_id % 740,
            "price_bucket": item_id % 10,
        }


class DeterministicRanker:
    def score(self, payload):
        return payload["candidate_item_id"].tolist(), [0.1, 0.9, 0.3]


class DeterministicRouter:
    def route(self, user_id: int):
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
                str(item_id): {
                    "category_id": item_id % 30,
                    "brand_id": item_id % 740,
                    "price_bucket": item_id % 10,
                }
                for item_id in candidates
            },
        )


def inference_settings(model_version: str = "split-test") -> InferenceApiSettings:
    return InferenceApiSettings("http://feature-api", 1.0, model_version, 1.0, 4, 1)


def test_feature_api_exposes_online_features_with_pydantic_validation() -> None:
    app = create_feature_app(FeatureApiSettings(False), DeterministicFeatureClient())
    with TestClient(app) as client:
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


def test_inference_api_calls_feature_service_then_ranks() -> None:
    app = create_inference_app(
        inference_settings(),
        feature_service=DeterministicFeatureService(),
        router=DeterministicRouter(),
    )
    with TestClient(app) as client:
        response = client.post(
            "/recommendations",
            json={"user_id": 42, "candidate_item_ids": [101, 102, 103], "top_k": 2},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["model_version"] == "split-test"
    assert body["ab_variant"] == "control"
    assert [item["item_id"] for item in body["items"]] == [102, 103]


def test_inference_api_returns_control_while_shadow_candidate_runs() -> None:
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
    app = create_inference_app(
        inference_settings("stable-v1"),
        feature_service=DeterministicFeatureService(),
        router=router,
        shadow_runner=runner,
    )
    with TestClient(app) as client:
        response = client.post(
            "/recommendations",
            json={"user_id": 42, "candidate_item_ids": [101, 102, 103], "top_k": 2},
        )

    assert response.json()["model_version"] == "stable-v1"
    assert response.json()["ab_variant"] == "control"
    assert [item["item_id"] for item in response.json()["items"]] == [102, 103]
    assert control.calls == 1
    assert candidate.calls == 1
