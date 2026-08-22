from __future__ import annotations

import httpx
import pytest
from recsys_recommendation_mcp.client import InferenceApiClient


@pytest.mark.asyncio
async def test_facade_sends_the_existing_inference_contract_unchanged() -> None:
    bodies: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(__import__("json").loads(request.content))
        return httpx.Response(
            200,
            json={
                "user_id": 1001,
                "model_version": "bst-v7",
                "ab_variant": "treatment",
                "items": [{"item_id": 800081, "score": 0.99}],
            },
            request=request,
        )

    client = InferenceApiClient(
        base_url="http://inference",
        request_timeout_seconds=7,
        total_deadline_seconds=15,
        transport=httpx.MockTransport(handler),
    )
    try:
        result = await client.recommend(
            user_id=1001, candidate_item_ids=[800080, 800081], top_k=1
        )
    finally:
        await client.aclose()
    assert bodies == [
        {
            "user_id": 1001,
            "candidate_item_ids": [800080, 800081],
            "top_k": 1,
        }
    ]
    assert result.items[0].item_id == 800081
