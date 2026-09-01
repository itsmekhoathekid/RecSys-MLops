from __future__ import annotations

from unittest.mock import AsyncMock, Mock

import pytest
from fastapi.testclient import TestClient

from recsys_inference_api.app import create_app
from recsys_inference_api.settings import InferenceApiSettings
from recsys_serving_common.contracts import (
    OnlineFeaturesRequest,
    OnlineFeaturesResponse,
)


class DeterministicFeatureService:
    async def fetch(self, request: OnlineFeaturesRequest) -> OnlineFeaturesResponse:
        candidates = request.candidate_item_ids or list(range(100, 100 + request.top_k))
        return OnlineFeaturesResponse(
            user_id=request.user_id,
            candidate_item_ids=candidates,
            user_sequence={
                "hist_item_ids": [1, 2, 3],
                "hist_event_type_ids": [1, 1, 2],
                "hist_category_ids": [4, 5, 6],
                "hist_brand_ids": [7, 8, 9],
                "hist_price_bucket_ids": [1, 2, 3],
                "hist_time_ids": [1, 2, 3],
            },
            item_features={
                str(item_id): {
                    "category_id": item_id % 30,
                    "brand_id": item_id % 740,
                    "price_bucket": item_id % 10,
                }
                for item_id in candidates
            },
        )


class DeterministicRanker:
    model_version = "deterministic-test"

    async def score(self, payload):
        candidates = payload["candidate_item_id"].tolist()
        return candidates, [float(index) for index in range(len(candidates))]


@pytest.fixture
def inference_api() -> TestClient:
    feature_implementation = DeterministicFeatureService()
    feature_service = Mock(spec=DeterministicFeatureService)
    feature_service.fetch = AsyncMock(side_effect=feature_implementation.fetch)

    ranker_implementation = DeterministicRanker()
    ranker = Mock(spec=DeterministicRanker, wraps=ranker_implementation)
    ranker.model_version = ranker_implementation.model_version
    settings = InferenceApiSettings(
        feature_api_url="http://feature-api",
        feature_api_timeout_seconds=1.0,
        model_version=ranker.model_version,
        shadow_timeout_seconds=1.0,
        shadow_queue_size=4,
        shadow_max_concurrency=1,
    )
    app = create_app(settings, feature_service=feature_service, router=ranker)
    with TestClient(app) as client:
        client.app.state.feature_service_mock = feature_service
        client.app.state.ranker_mock = ranker
        yield client
