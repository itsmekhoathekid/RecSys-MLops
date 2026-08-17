"""Feast candidate search, hard filtering, and grouped item ranking.

The vector database returns chunks. This service refills a bounded candidate
pool, enforces scalar constraints, groups by item, and emits at most two evidence
chunks. Item score is maximum cosine similarity with deterministic tie-breaks.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Protocol

from recsys_rag_runtime import QUERY_PREFIX, TextEncoder

from recsys_rag_api.contracts import (
    CandidateChunk,
    EvidenceChunk,
    RetrievalFilters,
    RetrievalRequest,
    RetrievalResponse,
    RetrievedItem,
)
from recsys_rag_api.pointer import ActivePointerManager


class CandidateSearch(Protocol):
    """Vector search boundary implemented by Feast and test fakes."""

    def search(
        self, *, feature_view: str, query_vector: list[float], top_k: int
    ) -> list[CandidateChunk]:
        """Return chunk candidates ordered by descending cosine score."""


def _matches(candidate: CandidateChunk, filters: RetrievalFilters) -> bool:
    if filters.brands and candidate.brand.casefold() not in {
        brand.casefold() for brand in filters.brands
    }:
        return False
    categories = [candidate.category_l1, candidate.category_l2, candidate.category_l3]
    if filters.category_prefix and categories[: len(filters.category_prefix)] != filters.category_prefix:
        return False
    if filters.min_current_price is not None and candidate.current_price < filters.min_current_price:
        return False
    if filters.max_current_price is not None and candidate.current_price > filters.max_current_price:
        return False
    if filters.in_stock is not None and candidate.in_stock != filters.in_stock:
        return False
    if filters.chunk_types and candidate.chunk_type not in filters.chunk_types:
        return False
    return True


class RetrievalService:
    """Encode one query and return deterministically grouped item evidence."""

    def __init__(
        self,
        *,
        encoder: TextEncoder,
        search: CandidateSearch,
        pointers: ActivePointerManager,
    ) -> None:
        self.encoder = encoder
        self.search = search
        self.pointers = pointers

    def retrieve(self, request: RetrievalRequest) -> RetrievalResponse:
        """Retrieve unique items while honoring every supplied hard constraint."""

        pointer = self.pointers.get()
        # E5 asymmetric retrieval requires different prefixes for indexed passages
        # and user queries; both sides share the exact same packaged model.
        vector = self.encoder.encode([f"{QUERY_PREFIX}{request.query}"])[0]
        pool = min(max(request.top_k_items * 20, 100), 500)
        filtered: list[CandidateChunk] = []
        while True:
            candidates = self.search.search(
                feature_view=pointer.feature_view,
                query_vector=vector,
                top_k=pool,
            )
            filtered = [
                candidate
                for candidate in candidates
                if _matches(candidate, request.filters)
            ]
            if len({candidate.item_id for candidate in filtered}) >= request.top_k_items or pool >= 500:
                break
            pool = min(pool * 2, 500)

        grouped: dict[int, list[CandidateChunk]] = defaultdict(list)
        for candidate in filtered:
            grouped[candidate.item_id].append(candidate)
        items: list[RetrievedItem] = []
        for item_id, chunks in grouped.items():
            chunks.sort(key=lambda chunk: (-chunk.score, chunk.chunk_id))
            best = chunks[0]
            # Maximum chunk cosine is the item score. Evidence is capped so one
            # verbose product cannot dominate the API payload.
            items.append(
                RetrievedItem(
                    item_id=item_id,
                    score=best.score,
                    brand=best.brand,
                    category_path=[
                        value
                        for value in (best.category_l1, best.category_l2, best.category_l3)
                        if value
                    ],
                    current_price=best.current_price,
                    in_stock=best.in_stock,
                    average_rating=best.average_rating,
                    evidence=[
                        EvidenceChunk(
                            chunk_id=chunk.chunk_id,
                            chunk_type=chunk.chunk_type,
                            source_key=chunk.source_key,
                            text=chunk.text,
                            score=chunk.score,
                        )
                        for chunk in chunks[:2]
                    ],
                )
            )
        items.sort(key=lambda item: (-item.score, -item.average_rating, item.item_id))
        return RetrievalResponse(
            query=request.query,
            pipeline_run_id=pointer.pipeline_run_id,
            items=items[: request.top_k_items],
        )


class FeastCandidateSearch:
    """Adapt Feast ``retrieve_online_documents_v2`` into typed candidates."""

    FIELDS = (
        "embedding",
        "item_id",
        "chunk_type",
        "source_key",
        "text",
        "brand",
        "category_l1",
        "category_l2",
        "category_l3",
        "current_price",
        "in_stock",
        "average_rating",
    )

    def __init__(self, feature_store: object) -> None:
        self.feature_store = feature_store

    def search(
        self, *, feature_view: str, query_vector: list[float], top_k: int
    ) -> list[CandidateChunk]:
        """Call Feast and coerce its column-oriented response to candidate rows."""

        response = self.feature_store.retrieve_online_documents_v2(
            features=[f"{feature_view}:{field}" for field in self.FIELDS],
            query=query_vector,
            top_k=top_k,
            distance_metric="COSINE",
        )
        values = response.to_dict()
        if not values:
            return []
        row_count = max((len(value) for value in values.values() if isinstance(value, list)), default=0)

        def column(name: str, index: int, default: object = "") -> object:
            value = values.get(name, values.get(f"{feature_view}:{name}", []))
            return value[index] if isinstance(value, list) and index < len(value) else default

        return [
            CandidateChunk(
                chunk_id=str(column("chunk_id", index)),
                item_id=int(column("item_id", index, 0)),
                chunk_type=str(column("chunk_type", index)),
                source_key=str(column("source_key", index)),
                text=str(column("text", index)),
                brand=str(column("brand", index)),
                category_l1=str(column("category_l1", index)),
                category_l2=str(column("category_l2", index)),
                category_l3=str(column("category_l3", index)),
                current_price=float(column("current_price", index, 0.0)),
                in_stock=str(column("in_stock", index, "false")).casefold() in {"true", "1"},
                average_rating=float(column("average_rating", index, 0.0)),
                score=float(column("distance", index, 0.0)),
            )
            for index in range(row_count)
        ]
