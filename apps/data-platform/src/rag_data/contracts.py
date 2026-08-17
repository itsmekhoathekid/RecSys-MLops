"""Strict contracts for generated and canonical RAG item documents.

The LLM can populate only :class:`GeneratedItemContent`; deterministic code
owns identifiers, taxonomy, price, stock, and ratings. Validation fails closed
so malformed model output is retried rather than written to the lake.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, field_validator


class StrictModel(BaseModel):
    """Base contract that rejects undeclared fields from every payload."""

    model_config = ConfigDict(extra="forbid")


class GeneratedReview(StrictModel):
    """Synthetic review text and its non-empty aspect sentiments."""

    content: str = Field(min_length=1)
    sentiment_aspects: dict[str, Literal["positive", "neutral", "negative"]] = Field(
        min_length=1
    )

    @field_validator("content")
    @classmethod
    def strip_content(cls, value: str) -> str:
        """Normalize review text and reject whitespace-only content."""

        value = value.strip()
        if not value:
            raise ValueError("content must not be blank")
        return value


class QnaPair(StrictModel):
    """One synthetic catalog question paired with a non-empty answer."""

    question: str = Field(min_length=1)
    answer: str = Field(min_length=1)

    @field_validator("question", "answer")
    @classmethod
    def strip_text(cls, value: str) -> str:
        """Normalize Q&A fields and reject whitespace-only values."""

        value = value.strip()
        if not value:
            raise ValueError("Q&A text must not be blank")
        return value


class GeneratedItemContent(StrictModel):
    """The exact six-field JSON Schema accepted from OrcaRouter."""

    _finish_reason: str | None = PrivateAttr(default=None)

    title: str = Field(min_length=1)
    description: str = Field(min_length=1)
    specifications: dict[str, str | int | float | bool] = Field(min_length=1)
    usage_instructions: str = Field(min_length=1)
    reviews: list[GeneratedReview] = Field(min_length=2, max_length=2)
    qna_pairs: list[QnaPair] = Field(min_length=1, max_length=1)

    @field_validator("title", "description", "usage_instructions")
    @classmethod
    def strip_text(cls, value: str) -> str:
        """Normalize generated prose and reject whitespace-only values."""

        value = value.strip()
        if not value:
            raise ValueError("generated text must not be blank")
        return value

    @property
    def finish_reason(self) -> str | None:
        """Expose provider completion status without serializing it as content."""

        return self._finish_reason

    def attach_finish_reason(self, finish_reason: str | None) -> "GeneratedItemContent":
        """Attach provider audit metadata and return this validated instance."""

        self._finish_reason = finish_reason
        return self


class StructuredMetadata(StrictModel):
    """Deterministic filterable metadata copied or mapped without an LLM."""

    brand: str = Field(min_length=1)
    category_path: list[str] = Field(min_length=1)
    current_price: Decimal = Field(ge=0)
    in_stock: bool
    stock_quantity: int = Field(ge=0)
    warranty_months: int = Field(gt=0)
    warehouse_location: str = Field(min_length=1)


class UnstructuredText(StrictModel):
    """Generated text fields used to render retrieval source units."""

    title: str = Field(min_length=1)
    description: str = Field(min_length=1)
    specifications: dict[str, str | int | float | bool] = Field(min_length=1)
    usage_instructions: str = Field(min_length=1)


class CanonicalReview(StrictModel):
    """A generated review with a deterministic identifier and rating."""

    review_id: str = Field(min_length=1)
    rating: int = Field(ge=1, le=5)
    content: str = Field(min_length=1)
    sentiment_aspects: dict[str, Literal["positive", "neutral", "negative"]] = Field(
        min_length=1
    )


class ReviewsAndQna(StrictModel):
    """Review aggregates plus exactly two reviews and one Q&A pair."""

    average_rating: float = Field(ge=1, le=5)
    total_reviews: int = Field(ge=0)
    sample_reviews: list[CanonicalReview] = Field(min_length=2, max_length=2)
    qna_pairs: list[QnaPair] = Field(min_length=1, max_length=1)


class CanonicalItemDocument(StrictModel):
    """Canonical raw-zone document consumed by semantic chunking."""

    item_id: int
    sku: str = Field(min_length=1)
    structured_metadata: StructuredMetadata
    unstructured_text: UnstructuredText
    reviews_and_qna: ReviewsAndQna


class FailureRecord(StrictModel):
    """Serializable per-item failure retained for resumable generation."""

    item_id: int
    error_type: str
    message: str
    attempts: int = Field(ge=1)
    finish_reason: str | None = None
    failed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class RunManifest(StrictModel):
    """Raw generation state and immutable schema/model provenance."""

    dataset_type: Literal["rag_item_documents"] = "rag_item_documents"
    schema_version: Literal[1] = 1
    catalog_mapping_version: str = "catalog_mapping_v1"
    synthetic_catalog: Literal[True] = True
    grounding_level: Literal["llm_generated_from_source_ids_and_mapped_taxonomy"] = (
        "llm_generated_from_source_ids_and_mapped_taxonomy"
    )
    run_id: str
    status: Literal["running", "partial", "complete"] = "running"
    source_count: int = Field(ge=0, default=0)
    generated_count: int = Field(ge=0, default=0)
    failed_count: int = Field(ge=0, default=0)
    finish_reason_counts: dict[str, int] = Field(default_factory=dict)
    model: str
    prompt_version: str
    started_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    def refreshed(self, **changes: Any) -> "RunManifest":
        """Return an updated manifest with a fresh UTC audit timestamp."""

        return self.model_copy(
            update={"updated_at": datetime.now(timezone.utc), **changes}
        )
