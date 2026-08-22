from __future__ import annotations

import httpx
import pytest
from mcp.server.fastmcp.exceptions import ToolError as McpToolError
from pydantic import ValidationError

from recsys_recommendation_mcp.client import InferenceApiClient
from recsys_recommendation_mcp.contracts import RecommendationResponse
from recsys_recommendation_mcp.errors import DownstreamError
from recsys_recommendation_mcp.policy import (
    build_request_payload,
    is_retryable_status,
    tool_result_status,
)
from recsys_recommendation_mcp.server import create_mcp_server


def test_contract_boundaries() -> None:
    valid = {
        "user_id": 1,
        "model_version": "v1",
        "items": [{"item_id": 1, "score": 0.25}],
    }
    assert RecommendationResponse.model_validate(valid).model_dump() == {
        **valid,
        "ab_variant": None,
        "ab_experiment_id": None,
    }
    for invalid in (
        {**valid, "user_id": 0},
        {**valid, "invented": True},
    ):
        with pytest.raises(ValidationError):
            RecommendationResponse.model_validate(invalid)
    assert DownstreamError("timeout", "recsys-inference-api", True, "late").as_dict() == {
        "code": "timeout",
        "service": "recsys-inference-api",
        "retryable": True,
        "message": "late",
    }
    assert build_request_payload(1, None, 10) == {"user_id": 1, "top_k": 10}
    assert build_request_payload(1, [3, 2], 2) == {
        "user_id": 1,
        "top_k": 2,
        "candidate_item_ids": [3, 2],
    }
    assert is_retryable_status(502) is True
    assert is_retryable_status(503) is True
    assert is_retryable_status(500) is False
    assert is_retryable_status(400) is False
    assert tool_result_status(RecommendationResponse.model_validate(valid)) == "success"
    assert tool_result_status(
        RecommendationResponse.model_validate({**valid, "items": []})
    ) == "empty"


@pytest.mark.asyncio
async def test_client_retry_and_preserved_ranking() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(503, request=request)
        return httpx.Response(
            200,
            json={
                "user_id": 1001,
                "model_version": "v1",
                "items": [
                    {"item_id": 2, "score": 0.9},
                    {"item_id": 1, "score": 0.8},
                ],
            },
            request=request,
        )

    client = InferenceApiClient(
        base_url="http://inference",
        request_timeout_seconds=1,
        total_deadline_seconds=2,
        transport=httpx.MockTransport(handler),
    )
    try:
        result = await client.recommend(
            user_id=1001, candidate_item_ids=[1, 2], top_k=2
        )
    finally:
        await client.aclose()
    assert len(requests) == 2
    assert [item.item_id for item in result.items] == [2, 1]
    assert requests[-1].content == (
        b'{"user_id":1001,"top_k":2,"candidate_item_ids":[1,2]}'
    )


@pytest.mark.asyncio
async def test_non_retryable_and_tool_typed_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, request=request)

    client = InferenceApiClient(
        base_url="http://inference",
        request_timeout_seconds=1,
        total_deadline_seconds=2,
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(DownstreamError) as caught:
            await client.recommend(user_id=1, candidate_item_ids=None, top_k=1)
        assert caught.value.retryable is False
        mcp = create_mcp_server(client)
        with pytest.raises(McpToolError):
            await mcp.call_tool("get_personalized_recommendations", {"user_id": 1})
    finally:
        await client.aclose()
