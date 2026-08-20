"""Strict Pydantic contracts for MCP tool outputs."""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field

UserId = Annotated[int, Field(ge=0)]
CandidateItemIds = Annotated[list[int] | None, Field(max_length=100)]
FeatureTopK = Annotated[int, Field(ge=1, le=100)]
ChunkId = Annotated[str, Field(min_length=1, max_length=512)]
RagQuery = Annotated[str, Field(min_length=1, max_length=1000)]
RagTopK = Annotated[int, Field(ge=1, le=20)]
RagFilters = Annotated[dict[str, Any] | None, Field()]


class StrictModel(BaseModel):
    """Reject undeclared tool response fields."""

    model_config = ConfigDict(extra="forbid")


class ToolError(StrictModel):
    """Portable typed error returned for a failed downstream source."""

    code: str
    service: str
    retryable: bool
    message: str


class CompositeContext(StrictModel):
    """Grounded user and RAG context with explicit partial-result metadata."""

    user_context: dict[str, Any] | None = None
    rag_context: dict[str, Any] | None = None
    partial: bool = False
    errors: list[ToolError] = Field(default_factory=list)
