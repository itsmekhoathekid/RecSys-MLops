from __future__ import annotations

import pytest
from mcp.server.fastmcp.exceptions import ToolError as McpToolError
from recsys_recommendation_mcp.contracts import RecommendationResponse
from recsys_recommendation_mcp.errors import DownstreamError
from recsys_recommendation_mcp.server import TOOL_NAMES, create_mcp_server


class InferenceClient:
    def __init__(self, error: DownstreamError | None = None) -> None:
        self.error = error
        self.calls: list[dict[str, object]] = []

    async def recommend(self, **kwargs: object) -> RecommendationResponse:
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return RecommendationResponse.model_validate(
            {
                "user_id": kwargs["user_id"],
                "model_version": "bst-v7",
                "ab_variant": "treatment",
                "ab_experiment_id": "ranking-2026",
                "items": [
                    {"item_id": 800081, "score": 0.91},
                    {"item_id": 800080, "score": 0.72},
                ],
            }
        )


class EmptyInferenceClient(InferenceClient):
    async def recommend(self, **kwargs: object) -> RecommendationResponse:
        self.calls.append(kwargs)
        return RecommendationResponse.model_validate(
            {"user_id": kwargs["user_id"], "model_version": "bst-v7", "items": []}
        )


@pytest.mark.asyncio
async def test_lists_one_tool_and_preserves_model_ranking() -> None:
    dependency = InferenceClient()
    mcp = create_mcp_server(dependency)

    assert tuple(tool.name for tool in await mcp.list_tools()) == TOOL_NAMES
    result = await mcp.call_tool(
        "get_personalized_recommendations",
        {"user_id": 1001, "candidate_item_ids": [800080, 800081], "top_k": 2},
    )

    text = result[0][0].text
    assert text.index("800081") < text.index("800080")
    assert dependency.calls == [
        {"user_id": 1001, "candidate_item_ids": [800080, 800081], "top_k": 2}
    ]


@pytest.mark.asyncio
async def test_downstream_failure_exposes_typed_error() -> None:
    dependency = InferenceClient(
        DownstreamError("http_503", "recsys-inference-api", True, "unavailable")
    )
    mcp = create_mcp_server(dependency)

    with pytest.raises(McpToolError) as caught:
        await mcp.call_tool("get_personalized_recommendations", {"user_id": 1001})
    message = str(caught.value)
    assert '"code": "http_503"' in message
    assert '"retryable": true' in message


@pytest.mark.asyncio
async def test_invalid_input_never_calls_downstream() -> None:
    dependency = InferenceClient()
    mcp = create_mcp_server(dependency)

    invalid_arguments = [
        {"user_id": 0},
        {"user_id": 1, "candidate_item_ids": []},
        {"user_id": 1, "candidate_item_ids": list(range(501))},
        {"user_id": 1, "top_k": 0},
        {"user_id": 1, "top_k": 101},
    ]
    for arguments in invalid_arguments:
        with pytest.raises(McpToolError):
            await mcp.call_tool("get_personalized_recommendations", arguments)
    assert dependency.calls == []


@pytest.mark.asyncio
async def test_empty_ranking_is_a_valid_tool_result() -> None:
    mcp = create_mcp_server(EmptyInferenceClient())
    result = await mcp.call_tool("get_personalized_recommendations", {"user_id": 1001})
    assert '"items":[]' in result[0][0].text.replace(" ", "")
