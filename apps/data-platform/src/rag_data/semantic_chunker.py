"""Structure-aware semantic chunking for canonical item documents.

Section boundaries are never crossed. Short review/Q&A units remain atomic;
long units are sentence-split at low adjacent-sentence cosine similarity. Output
IDs are stable across content edits while content hashes drive incremental work.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Sequence

import numpy as np

from rag_data.contracts import CanonicalItemDocument
from rag_data.embedding import PASSAGE_PREFIX, TextEncoder
from rag_data.pipeline_contracts import ChunkType, ItemChunk


SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?…])\s+|\n+")


@dataclass(frozen=True)
class ChunkerConfig:
    """Token and semantic-breakpoint controls for V1 chunking."""

    target_tokens: int = 240
    min_tokens: int = 80
    max_tokens: int = 384
    overlap_tokens: int = 32
    breakpoint_percentile: float = 20.0
    version: str = "semantic_chunker_v1"


@dataclass(frozen=True)
class SourceUnit:
    """A hard-boundary source section before semantic splitting."""

    chunk_type: ChunkType
    source_key: str
    text: str


def _sha256(value: str) -> str:
    return f"sha256:{hashlib.sha256(value.encode('utf-8')).hexdigest()}"


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def render_source_units(item: CanonicalItemDocument) -> list[SourceUnit]:
    """Render one item into hard-boundary overview/spec/usage/review/Q&A units."""

    text = item.unstructured_text
    specifications = "\n".join(
        f"{key}: {text.specifications[key]}" for key in sorted(text.specifications)
    )
    units = [
        SourceUnit(
            "product_overview", "overview", f"{text.title}\n{text.description}"
        ),
        SourceUnit("specifications", "specifications", specifications),
        SourceUnit("usage_instructions", "usage", text.usage_instructions),
    ]
    units.extend(
        SourceUnit("review", review.review_id, review.content)
        for review in item.reviews_and_qna.sample_reviews
    )
    units.extend(
        SourceUnit("qna", f"qna_{index:02d}", f"Hỏi: {pair.question}\nĐáp: {pair.answer}")
        for index, pair in enumerate(item.reviews_and_qna.qna_pairs, start=1)
    )
    return [unit for unit in units if unit.text.strip()]


class SemanticChunker:
    """Split item sections using token limits and semantic sentence boundaries."""

    def __init__(self, encoder: TextEncoder, config: ChunkerConfig) -> None:
        self.encoder = encoder
        self.config = config

    def _sentences(self, text: str) -> list[str]:
        return [part.strip() for part in SENTENCE_BOUNDARY.split(text) if part.strip()]

    def _semantic_breaks(self, sentences: Sequence[str]) -> set[int]:
        if len(sentences) < 2:
            return set()
        vectors = np.asarray(self.encoder.encode(sentences), dtype=np.float32)
        similarities = np.sum(vectors[:-1] * vectors[1:], axis=1)
        # Low adjacent-sentence similarity is the natural topic transition. The
        # percentile is computed per source unit so long reviews do not influence
        # product-description boundaries (sections are hard boundaries).
        threshold = float(
            np.percentile(similarities, self.config.breakpoint_percentile)
        )
        return {
            index + 1
            for index, similarity in enumerate(similarities)
            if float(similarity) <= threshold
        }

    def _split_unit(self, unit: SourceUnit, *, max_tokens: int | None = None) -> list[str]:
        hard_limit = max_tokens or self.config.max_tokens
        if self.encoder.token_count(unit.text) <= hard_limit:
            return [unit.text]
        sentences = self._sentences(unit.text)
        if len(sentences) == 1:
            words = unit.text.split()
            # This emergency path is only for unpunctuated text; model token count
            # is rechecked and the window shrinks until the hard limit is satisfied.
            chunks: list[str] = []
            cursor = 0
            while cursor < len(words):
                end = min(len(words), cursor + self.config.target_tokens)
                while end > cursor and self.encoder.token_count(" ".join(words[cursor:end])) > hard_limit:
                    end -= 1
                if end == cursor:
                    raise ValueError("A single token exceeds the configured hard limit")
                chunks.append(" ".join(words[cursor:end]))
                cursor = max(end - self.config.overlap_tokens, cursor + 1)
            return chunks

        semantic_breaks = self._semantic_breaks(sentences)
        chunks: list[str] = []
        current: list[str] = []
        for index, sentence in enumerate(sentences):
            candidate = " ".join([*current, sentence])
            candidate_tokens = self.encoder.token_count(candidate)
            should_break = bool(current) and (
                candidate_tokens > hard_limit
                or (
                    index in semantic_breaks
                    and self.encoder.token_count(" ".join(current))
                    >= self.config.min_tokens
                )
                or self.encoder.token_count(" ".join(current)) >= self.config.target_tokens
            )
            if should_break:
                chunks.append(" ".join(current))
                # Sentence overlap is capped by token count, preserving context
                # without crossing the source-unit/section boundary.
                overlap: list[str] = []
                for previous in reversed(current):
                    candidate_overlap = [previous, *overlap]
                    if self.encoder.token_count(" ".join(candidate_overlap)) > self.config.overlap_tokens:
                        break
                    overlap = candidate_overlap
                current = overlap
            current.append(sentence)
        if current:
            chunks.append(" ".join(current))
        if any(self.encoder.token_count(chunk) > hard_limit for chunk in chunks):
            raise ValueError("Semantic splitter produced a chunk above the hard limit")
        return chunks

    def chunk_item(
        self,
        item: CanonicalItemDocument,
        *,
        source_run_id: str,
        event_timestamp: datetime | None = None,
    ) -> list[ItemChunk]:
        """Create deterministic chunks for one item without external side effects."""

        item_payload = item.model_dump(mode="json")
        item_hash = _sha256(_canonical_json(item_payload))
        categories = [*item.structured_metadata.category_path, "", ""]
        context = (
            f"Tiêu đề: {item.unstructured_text.title}. "
            f"Thương hiệu: {item.structured_metadata.brand}. "
            f"Danh mục: {' > '.join(item.structured_metadata.category_path)}."
        )
        created_at = event_timestamp or datetime.now(timezone.utc)
        output: list[ItemChunk] = []
        for unit in render_source_units(item):
            prefix = f"{PASSAGE_PREFIX}{context}\n"
            available_tokens = self.config.max_tokens - self.encoder.token_count(prefix)
            if available_tokens < self.config.min_tokens:
                raise ValueError("Embedding context leaves too few tokens for item content")
            for part_index, text in enumerate(
                self._split_unit(unit, max_tokens=available_tokens)
            ):
                normalized_text = text.strip()
                # Price, stock, and warehouse are volatile hard constraints. They
                # remain scalar metadata so changing inventory never shifts semantic
                # meaning or requires an otherwise unnecessary re-embedding.
                embedding_text = f"{prefix}{normalized_text}"
                # IDs deliberately exclude content hashes. Changed content upserts
                # the same entity; removed IDs are handled by reconciliation.
                chunk_id = f"{item.item_id}:{unit.chunk_type}:{unit.source_key}:{part_index}"
                output.append(
                    ItemChunk(
                        chunk_id=chunk_id,
                        item_id=item.item_id,
                        chunk_type=unit.chunk_type,
                        source_key=unit.source_key,
                        chunk_index=part_index,
                        text=normalized_text,
                        embedding_text=embedding_text,
                        token_count=self.encoder.token_count(embedding_text),
                        content_hash=_sha256(normalized_text),
                        item_content_hash=item_hash,
                        brand=item.structured_metadata.brand,
                        category_l1=categories[0],
                        category_l2=categories[1],
                        category_l3=categories[2],
                        current_price=float(item.structured_metadata.current_price),
                        in_stock=item.structured_metadata.in_stock,
                        average_rating=item.reviews_and_qna.average_rating,
                        source_run_id=source_run_id,
                        event_timestamp=created_at,
                    )
                )
        return output

    def chunk_items(
        self, items: Iterable[CanonicalItemDocument], *, source_run_id: str
    ) -> list[ItemChunk]:
        """Chunk items and reject duplicate stable chunk IDs."""

        chunks = [
            chunk
            for item in items
            for chunk in self.chunk_item(item, source_run_id=source_run_id)
        ]
        ids = [chunk.chunk_id for chunk in chunks]
        if len(ids) != len(set(ids)):
            raise ValueError("Duplicate chunk IDs generated")
        return chunks
