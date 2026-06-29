from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class RecommendationRequest(BaseModel):
    user_id: int = Field(ge=1)
    candidate_item_ids: list[int] | None = Field(default=None, min_length=1, max_length=500)
    top_k: int = Field(default=10, ge=1, le=100)


class RecommendationItem(BaseModel):
    item_id: int
    score: float


class RecommendationResponse(BaseModel):
    user_id: int
    model_version: str
    ab_variant: str | None = None
    ab_experiment_id: str | None = None
    items: list[RecommendationItem]


class OnlineFeaturesResponse(BaseModel):
    user_id: int
    candidate_item_ids: list[int]
    user_sequence: dict[str, Any]
    item_features: dict[str, dict[str, Any]]
