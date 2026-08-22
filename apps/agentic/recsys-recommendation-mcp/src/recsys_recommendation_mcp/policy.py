"""Pure recommendation transport policy shared by client and MCP tool."""

from __future__ import annotations

from typing import Any

from recsys_recommendation_mcp.contracts import RecommendationResponse

TRANSIENT_HTTP_STATUSES = frozenset({502, 503})


def build_request_payload(
    user_id: int, candidate_item_ids: list[int] | None, top_k: int
) -> dict[str, Any]:
    """Build the existing inference API request without synthetic fields."""

    payload: dict[str, Any] = {"user_id": user_id, "top_k": top_k}
    if candidate_item_ids is not None:
        payload["candidate_item_ids"] = candidate_item_ids
    return payload


def is_retryable_status(status_code: int) -> bool:
    """Classify only gateway and capacity failures as retryable HTTP errors."""

    return status_code in TRANSIENT_HTTP_STATUSES


def tool_result_status(response: RecommendationResponse) -> str:
    """Separate valid empty rankings from successful non-empty rankings."""

    return "success" if response.items else "empty"
