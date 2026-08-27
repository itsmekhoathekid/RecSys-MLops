"""FastMCP tool registration over the two RecSys serving APIs."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from recsys_feature_rag_mcp.clients.online_features import OnlineFeatureClient
from recsys_feature_rag_mcp.clients.rag import RagClient
from recsys_feature_rag_mcp.contracts import (
    CandidateItemIds,
    ChunkId,
    CompositeContext,
    FeatureTopK,
    RagFilters,
    RagQuery,
    RagTopK,
    ToolError,
    UserId,
)
from recsys_feature_rag_mcp.errors import DownstreamError
from recsys_feature_rag_mcp.observability import (
    PARTIAL_RESULTS,
    TOOL_CALLS,
    TOOL_DURATION,
)

TOOL_NAMES = (
    "get_user_online_features",
    "get_chunk_by_id",
    "retrieve_rag_context",
    "build_user_rag_context",
)


def _optional_candidate_ids(candidate_item_ids: CandidateItemIds) -> list[int] | None:
    """Treat the model-emitted empty array as the optional-field sentinel."""

    return candidate_item_ids or None


def create_mcp_server(
    feature_client: OnlineFeatureClient,
    rag_client: RagClient,
    allowed_hosts: tuple[str, ...] = ("localhost:*", "127.0.0.1:*", "[::1]:*"),
) -> FastMCP:
    """Register the four versioned RecSys context tools on FastMCP."""

    mcp = FastMCP(
        "RecSys Feature and RAG MCP",
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=list(allowed_hosts),
        ),
    )

    async def observed(name: str, operation: Awaitable[Any]) -> Any:
        """Record tool latency and terminal status around an awaitable."""

        with TOOL_DURATION.labels(name).time():
            try:
                result = await operation
            except Exception:
                TOOL_CALLS.labels(name, "error").inc()
                raise
        TOOL_CALLS.labels(name, "success").inc()
        return result

    @mcp.tool()
    async def get_user_online_features(
        user_id: UserId,
        candidate_item_ids: CandidateItemIds = None,
        top_k: FeatureTopK = 10,
    ) -> dict[str, Any]:
        """Get materialized user and candidate item features for one user ID."""

        return await observed(
            "get_user_online_features",
            feature_client.get_features(
                user_id=user_id,
                candidate_item_ids=_optional_candidate_ids(candidate_item_ids),
                top_k=top_k,
            ),
        )

    @mcp.tool()
    async def get_chunk_by_id(chunk_id: ChunkId) -> dict[str, Any]:
        """Get one materialized RAG chunk from the active index by stable ID."""

        return await observed("get_chunk_by_id", rag_client.get_chunk(chunk_id))

    @mcp.tool()
    async def retrieve_rag_context(
        query: RagQuery,
        top_k_items: RagTopK = 10,
        filters: RagFilters = None,
    ) -> dict[str, Any]:
        """Retrieve item-grouped semantic evidence for a natural-language query."""

        return await observed(
            "retrieve_rag_context",
            rag_client.retrieve(
                query=query,
                top_k_items=top_k_items,
                filters=filters,
            ),
        )

    @mcp.tool()
    async def build_user_rag_context(
        user_id: UserId,
        query: RagQuery,
        candidate_item_ids: CandidateItemIds = None,
        top_k: FeatureTopK = 10,
        top_k_items: RagTopK = 10,
        filters: RagFilters = None,
    ) -> dict[str, Any]:
        """Build user features and semantic evidence concurrently for one answer."""

        async def combine() -> dict[str, Any]:
            """Run feature and RAG calls concurrently with partial semantics."""

            results = await asyncio.gather(
                feature_client.get_features(
                    user_id=user_id,
                    candidate_item_ids=_optional_candidate_ids(candidate_item_ids),
                    top_k=top_k,
                ),
                rag_client.retrieve(
                    query=query,
                    top_k_items=top_k_items,
                    filters=filters,
                ),
                return_exceptions=True,
            )
            errors = [result for result in results if isinstance(result, Exception)]
            if len(errors) == 2:
                payload = [
                    error.as_dict()
                    if isinstance(error, DownstreamError)
                    else {
                        "code": "internal",
                        "service": "mcp",
                        "retryable": False,
                        "message": "context construction failed",
                    }
                    for error in errors
                ]
                raise RuntimeError(json.dumps(payload, sort_keys=True))
            typed_errors = [
                ToolError.model_validate(error.as_dict())
                if isinstance(error, DownstreamError)
                else ToolError(
                    code="internal",
                    service="mcp",
                    retryable=False,
                    message="context construction partially failed",
                )
                for error in errors
            ]
            partial = bool(typed_errors)
            if partial:
                PARTIAL_RESULTS.inc()
            response = CompositeContext(
                user_context=results[0] if isinstance(results[0], dict) else None,
                rag_context=results[1] if isinstance(results[1], dict) else None,
                partial=partial,
                errors=typed_errors,
            )
            return response.model_dump()

        return await observed("build_user_rag_context", combine())

    return mcp
