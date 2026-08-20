"""Strict HTTP and internal contracts for item retrieval.

The public API returns item-grouped evidence, never raw vector-store rows. Price,
stock, category, brand, and chunk-type constraints remain scalar filters and are
not encoded into the semantic query vector.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


ChunkType = Literal[
    "product_overview", "specifications", "usage_instructions", "review", "qna"
]


class StrictModel(BaseModel):
    """Forbid forward-incompatible fields at the API boundary."""

    model_config = ConfigDict(extra="forbid")


class RetrievalFilters(StrictModel):
    """Optional hard constraints applied after vector candidate retrieval."""

    brands: list[str] | None = None
    category_prefix: list[str] | None = Field(default=None, max_length=3)
    min_current_price: float | None = Field(default=None, ge=0)
    max_current_price: float | None = Field(default=None, ge=0)
    in_stock: bool | None = None
    chunk_types: list[ChunkType] | None = None

    @field_validator("brands")
    @classmethod
    def brands_must_not_be_blank(cls, value: list[str] | None) -> list[str] | None:
        """Normalize brand filters while rejecting empty strings."""

        if value is None:
            return None
        normalized = [brand.strip() for brand in value]
        if not normalized or any(not brand for brand in normalized):
            raise ValueError("brands must contain non-blank values")
        return normalized


class RetrievalRequest(StrictModel):
    """Semantic query and desired number of unique items."""

    query: str = Field(min_length=1, max_length=1000)
    top_k_items: int = Field(default=10, ge=1, le=20)
    filters: RetrievalFilters = Field(default_factory=RetrievalFilters)

    @field_validator("query")
    @classmethod
    def query_must_not_be_blank(cls, value: str) -> str:
        """Trim the query before applying E5's ``query:`` prefix."""

        value = value.strip()
        if not value:
            raise ValueError("query must not be blank")
        return value


class CandidateChunk(StrictModel):
    """Normalized result row returned by the Feast search adapter."""

    chunk_id: str
    item_id: int
    chunk_type: ChunkType
    source_key: str
    text: str
    brand: str
    category_l1: str = ""
    category_l2: str = ""
    category_l3: str = ""
    current_price: float
    in_stock: bool
    average_rating: float
    score: float


class EvidenceChunk(StrictModel):
    """One supporting passage returned for a grouped item."""

    chunk_id: str
    chunk_type: ChunkType
    source_key: str
    text: str
    score: float


class RetrievedItem(StrictModel):
    """Unique item ranked by its strongest semantic evidence chunk."""

    item_id: int
    score: float
    brand: str
    category_path: list[str]
    current_price: float
    in_stock: bool
    average_rating: float
    evidence: list[EvidenceChunk] = Field(min_length=1, max_length=2)


class RetrievalResponse(StrictModel):
    """Grouped item response with the active index release identifier."""

    query: str
    pipeline_run_id: str
    items: list[RetrievedItem]


class ChunkRecord(StrictModel):
    """One exact online-store chunk without its retrieval embedding."""

    chunk_id: str
    item_id: int
    chunk_type: ChunkType
    source_key: str
    text: str
    brand: str
    category_l1: str = ""
    category_l2: str = ""
    category_l3: str = ""
    current_price: float
    in_stock: bool
    average_rating: float
    source_run_id: str


class ChunkResponse(ChunkRecord):
    """Exact chunk response annotated with the active index release."""

    pipeline_run_id: str


class ChunkBatchRequest(StrictModel):
    """Bounded, duplicate-free exact chunk lookup request."""

    chunk_ids: list[str] = Field(min_length=1, max_length=100)

    @field_validator("chunk_ids")
    @classmethod
    def chunk_ids_must_be_unique_and_non_blank(cls, value: list[str]) -> list[str]:
        normalized = [chunk_id.strip() for chunk_id in value]
        if any(not chunk_id for chunk_id in normalized):
            raise ValueError("chunk_ids must contain non-blank values")
        if len(normalized) != len(set(normalized)):
            raise ValueError("chunk_ids must be unique")
        return normalized


class ChunkBatchResponse(StrictModel):
    """Ordered exact chunks plus IDs absent from the active online view."""

    pipeline_run_id: str
    chunks: list[ChunkRecord]
    missing_chunk_ids: list[str]
