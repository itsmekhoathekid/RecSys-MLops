"""Contracts for item chunk, embedding, index, and active-pointer artifacts.

Inputs are complete canonical item manifests and documents. Outputs are strict,
versioned records written to the silver and gold lake zones. The models reject
unknown fields so a producer cannot silently publish a contract the retrieval
service does not understand.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Literal

from pydantic import Field, field_validator, model_validator

from rag_data.contracts import StrictModel


ChunkType = Literal[
    "product_overview", "specifications", "usage_instructions", "review", "qna"
]


class ItemChunk(StrictModel):
    """A stable semantic unit derived from one canonical item document."""

    chunk_id: str = Field(min_length=1)
    item_id: int
    chunk_type: ChunkType
    source_key: str = Field(min_length=1)
    chunk_index: int = Field(ge=0)
    text: str = Field(min_length=1)
    embedding_text: str = Field(min_length=1)
    token_count: int = Field(gt=0, le=384)
    content_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    item_content_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    brand: str = Field(min_length=1)
    category_l1: str = ""
    category_l2: str = ""
    category_l3: str = ""
    current_price: float = Field(ge=0)
    in_stock: bool
    average_rating: float = Field(ge=1, le=5)
    source_run_id: str = Field(min_length=1)
    event_timestamp: datetime

    @field_validator("embedding_text")
    @classmethod
    def passage_prefix_is_required(cls, value: str) -> str:
        """Ensure indexed documents use the E5 passage-side contract."""

        if not value.startswith("passage: "):
            raise ValueError("embedding_text must start with 'passage: '")
        return value


class EmbeddedItemChunk(ItemChunk):
    """An item chunk enriched with a finite, normalized 384-D vector."""

    embedding: list[float] = Field(min_length=384, max_length=384)

    @field_validator("embedding")
    @classmethod
    def embedding_is_finite_and_normalized(cls, value: list[float]) -> list[float]:
        """Reject corrupt vectors before they reach Feast or Milvus."""

        if not all(math.isfinite(component) for component in value):
            raise ValueError("embedding contains NaN or infinity")
        norm = math.sqrt(sum(component * component for component in value))
        if not 0.999 <= norm <= 1.001:
            raise ValueError(f"embedding L2 norm must be ~1.0, got {norm:.6f}")
        return value


class ArtifactManifest(StrictModel):
    """Checkpoint and completion metadata for silver or gold artifacts."""

    dataset_type: Literal["rag_item_chunks", "rag_item_embeddings"]
    schema_version: Literal[1] = 1
    run_id: str = Field(min_length=1)
    source_run_id: str = Field(min_length=1)
    status: Literal["running", "partial", "complete"] = "running"
    record_count: int = Field(ge=0, default=0)
    unique_item_count: int = Field(ge=0, default=0)
    failed_count: int = Field(ge=0, default=0)
    chunker_version: str
    embedding_model: str
    embedding_revision: str
    embedding_dimension: Literal[384] = 384
    model_checksum: str = Field(min_length=1)
    content_hashes: dict[str, str] = Field(default_factory=dict)
    started_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    def refreshed(self, **changes: object) -> "ArtifactManifest":
        """Return a timestamped immutable-style manifest update."""

        return self.model_copy(
            update={"updated_at": datetime.now(timezone.utc), **changes}
        )


class IndexManifest(StrictModel):
    """Evidence produced while validating a candidate Milvus collection."""

    pipeline_run_id: str
    slot: Literal["blue", "green"]
    feature_view: str
    collection_name: str
    status: Literal["candidate", "validated", "published", "failed"]
    vector_count: int = Field(ge=0)
    unique_item_count: int = Field(ge=0)
    embedding_dimension: Literal[384] = 384
    retrieval_smoke_passed: bool = False
    validated_at: datetime | None = None


class ActiveIndexPointer(StrictModel):
    """The atomically published embedding contract consumed by the API."""

    active_slot: Literal["blue", "green"]
    feature_view: str
    pipeline_run_id: str
    source_run_id: str
    chunker_version: str
    embedding_model: str
    embedding_revision: str
    embedding_dimension: Literal[384] = 384
    published_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    previous_slot: Literal["blue", "green"] | None = None
    previous_pipeline_run_id: str | None = None

    @model_validator(mode="after")
    def previous_slot_differs_from_active(self) -> "ActiveIndexPointer":
        """Prevent a rollback pointer from referring to the active slot."""

        if self.previous_slot == self.active_slot:
            raise ValueError("previous_slot must differ from active_slot")
        return self
