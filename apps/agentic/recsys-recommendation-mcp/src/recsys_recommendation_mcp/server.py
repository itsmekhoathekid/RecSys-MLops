"""FastMCP registration for the single recommendation tool."""

from __future__ import annotations

import json

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from recsys_recommendation_mcp.client import InferenceApiClient
from recsys_recommendation_mcp.contracts import (
    CandidateItemIds,
    RecommendationResponse,
    TopK,
    UserId,
)
from recsys_recommendation_mcp.errors import DownstreamError
from recsys_recommendation_mcp.observability import TOOL_CALLS, TOOL_DURATION
from recsys_recommendation_mcp.policy import tool_result_status

TOOL_NAMES = ("get_personalized_recommendations",)


def create_mcp_server(
    inference_client: InferenceApiClient,
    allowed_hosts: tuple[str, ...] = ("localhost:*", "127.0.0.1:*", "[::1]:*"),
) -> FastMCP:
    """Expose the inference API through one stateless MCP tool."""

    mcp = FastMCP(
        "RecSys Recommendation MCP",
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=list(allowed_hosts),
        ),
    )

    @mcp.tool()
    async def get_personalized_recommendations(
        user_id: UserId,
        candidate_item_ids: CandidateItemIds = None,
        top_k: TopK = 10,
    ) -> dict[str, object]:
        """Get model-ranked Top-K items without changing order or scores."""

        with TOOL_DURATION.time():
            try:
                response: RecommendationResponse = await inference_client.recommend(
                    user_id=user_id,
                    candidate_item_ids=candidate_item_ids,
                    top_k=top_k,
                )
            except DownstreamError as exc:
                TOOL_CALLS.labels("error").inc()
                raise RuntimeError(json.dumps(exc.as_dict(), sort_keys=True)) from exc
        TOOL_CALLS.labels(tool_result_status(response)).inc()
        return response.model_dump()

    return mcp
