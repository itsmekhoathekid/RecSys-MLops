from __future__ import annotations

from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from recsys_online_feature_api.app import create_app
from recsys_online_feature_api.settings import FeatureApiSettings


class DeterministicFeatureClient:
    def _feature_store(self) -> str:
        return "deterministic-store"

    def close(self) -> None:
        pass

    def candidates(self, user_id: int, limit: int) -> list[int]:
        return list(range(100, 100 + limit))

    def user_sequence(self, user_id: int) -> dict[str, list[int]]:
        return {
            "hist_item_ids": [user_id, 2, 3],
            "hist_event_type_ids": [1, 1, 2],
        }

    def item_features_batch(self, item_ids: list[int]) -> dict[str, dict[str, int]]:
        return {
            str(item_id): {
                "category_id": item_id % 30,
                "brand_id": item_id % 740,
                "price_bucket": item_id % 10,
            }
            for item_id in item_ids
        }


@pytest.fixture
def online_feature_api() -> TestClient:
    implementation = DeterministicFeatureClient()
    feature_client = Mock(spec=DeterministicFeatureClient, wraps=implementation)
    app = create_app(
        FeatureApiSettings(warmup_on_startup=False),
        feature_client=feature_client,
    )
    with TestClient(app) as client:
        client.app.state.feature_client_mock = feature_client
        yield client
