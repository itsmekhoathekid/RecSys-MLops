from __future__ import annotations

import json

import pytest
from mcp.server.fastmcp.exceptions import ToolError as McpToolError
from recsys_feature_rag_mcp.errors import DownstreamError
from recsys_feature_rag_mcp.server import TOOL_NAMES, create_mcp_server


class FeatureClient:
    def __init__(self, error: DownstreamError | None = None):
        self.error = error
        self.calls = []

    async def get_features(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return {"user_id": kwargs["user_id"], "candidate_item_ids": []}


class RagClient:
    def __init__(self, error: DownstreamError | None = None):
        self.error = error

    async def get_chunk(self, chunk_id):
        if self.error:
            raise self.error
        return {"chunk_id": chunk_id, "text": "evidence"}

    async def retrieve(self, **kwargs):
        if self.error:
            raise self.error
        return {"query": kwargs["query"], "items": []}


@pytest.mark.asyncio
async def test_mcp_lists_exact_contract_tools_and_calls_them():
    mcp = create_mcp_server(FeatureClient(), RagClient())
    tools = await mcp.list_tools()
    assert tuple(tool.name for tool in tools) == TOOL_NAMES

    result = await mcp.call_tool("get_chunk_by_id", {"chunk_id": "chunk-1"})
    assert "chunk-1" in result[0][0].text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool_name", ["get_user_online_features", "build_user_rag_context"]
)
async def test_empty_candidate_ids_are_normalized_to_optional_field(tool_name):
    feature_client = FeatureClient()
    mcp = create_mcp_server(feature_client, RagClient())
    arguments = {"user_id": 7, "candidate_item_ids": [], "top_k": 2}
    if tool_name == "build_user_rag_context":
        arguments.update({"query": "headphones", "top_k_items": 2})

    await mcp.call_tool(tool_name, arguments)

    assert feature_client.calls == [
        {"user_id": 7, "candidate_item_ids": None, "top_k": 2}
    ]


@pytest.mark.asyncio
async def test_composite_returns_partial_result_for_one_downstream_error():
    error = DownstreamError("timeout", "recsys-rag-api", True, "timed out")
    mcp = create_mcp_server(FeatureClient(), RagClient(error))

    result = await mcp.call_tool(
        "build_user_rag_context", {"user_id": 7, "query": "headphones"}
    )
    payload = json.loads(result[0][0].text)
    assert payload["partial"] is True
    assert payload["user_context"]["user_id"] == 7
    assert payload["rag_context"] is None
    assert payload["errors"][0]["retryable"] is True


@pytest.mark.asyncio
async def test_composite_fails_when_both_downstreams_fail():
    error = DownstreamError("timeout", "downstream", True, "timed out")
    mcp = create_mcp_server(FeatureClient(error), RagClient(error))

    with pytest.raises(Exception):  # noqa: B017 - FastMCP wraps tool failures.
        await mcp.call_tool(
            "build_user_rag_context", {"user_id": 7, "query": "headphones"}
        )


@pytest.mark.asyncio
async def test_direct_tool_failure_exposes_the_typed_error_contract():
    error = DownstreamError("timeout", "recsys-rag-api", True, "timed out")
    mcp = create_mcp_server(FeatureClient(), RagClient(error))

    with pytest.raises(McpToolError) as caught:
        await mcp.call_tool("get_chunk_by_id", {"chunk_id": "chunk-1"})
    serialized = str(caught.value)
    assert '"code": "timeout"' in serialized
    assert '"service": "recsys-rag-api"' in serialized
    assert '"retryable": true' in serialized
