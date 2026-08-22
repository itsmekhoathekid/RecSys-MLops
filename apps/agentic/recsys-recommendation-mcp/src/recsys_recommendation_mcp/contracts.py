"""Strict recommendation MCP input and output contracts."""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

UserId = Annotated[int, Field(ge=1)]
ItemId = int
CandidateList = Annotated[list[ItemId], Field(min_length=1, max_length=500)]
CandidateItemIds = CandidateList | None
TopK = Annotated[int, Field(ge=1, le=100)]


class StrictModel(BaseModel):
    """Reject fields that are not part of the public recommendation contract."""

    model_config = ConfigDict(extra="forbid")


class RecommendationItem(StrictModel):
    """One item and its immutable model score."""

    item_id: ItemId
    score: float


class RecommendationResponse(StrictModel):
    """Pass-through response returned by the inference API."""

    user_id: UserId
    model_version: str
    ab_variant: str | None = None
    ab_experiment_id: str | None = None
    items: list[RecommendationItem]


class ToolError(StrictModel):
    """Portable downstream error surfaced to the agent runtime."""

    code: str
    service: str
    retryable: bool
    message: str
