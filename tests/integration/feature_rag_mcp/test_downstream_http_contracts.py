from __future__ import annotations

import httpx
import pytest

from recsys_feature_rag_mcp.clients.online_features import OnlineFeatureClient
from recsys_feature_rag_mcp.clients.rag import RagClient


@pytest.mark.asyncio
async def test_both_downstream_public_http_contracts_via_mock_transport():
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        if request.url.path == "/online-features":
            return httpx.Response(200, json={"user_id": 1001}, request=request)
        if request.url.path == "/v1/rag/chunks/chunk-1":
            return httpx.Response(
                200,
                json={"chunk_id": "chunk-1", "pipeline_run_id": "run-1"},
                request=request,
            )
        return httpx.Response(
            200,
            json={"query": "headphones", "items": []},
            request=request,
        )

    transport = httpx.MockTransport(handler)
    feature = OnlineFeatureClient(
        base_url="http://feature",
        timeout_seconds=2,
        transport=transport,
    )
    rag = RagClient(
        base_url="http://rag",
        timeout_seconds=5,
        transport=transport,
    )
    try:
        await feature.get_features(
            user_id=1001,
            candidate_item_ids=[1, 2],
            top_k=2,
        )
        await rag.get_chunk("chunk-1")
        await rag.retrieve(query="headphones", top_k_items=10, filters=None)
    finally:
        await feature.aclose()
        await rag.aclose()

    assert seen == [
        ("POST", "/online-features"),
        ("GET", "/v1/rag/chunks/chunk-1"),
        ("POST", "/v1/rag/retrieve"),
    ]
