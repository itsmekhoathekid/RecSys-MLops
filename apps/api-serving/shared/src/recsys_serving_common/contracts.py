from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class OnlineFeaturesResponse(BaseModel):
    user_id: int
    candidate_item_ids: list[int]
    user_sequence: dict[str, Any]
    item_features: dict[str, dict[str, Any]]


class OnlineFeaturesRequest(BaseModel):
    user_id: int = Field(ge=1)
    candidate_item_ids: list[int] | None = Field(
        default=None, min_length=1, max_length=500
    )
    top_k: int = Field(default=10, ge=1, le=100)
