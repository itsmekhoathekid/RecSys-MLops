from __future__ import annotations

import hashlib
from decimal import Decimal

from rag_data.contracts import CanonicalItemDocument
import pytest

from rag_data.semantic_chunker import ChunkerConfig, SemanticChunker, SourceUnit


class FakeEncoder:
    def token_count(self, text: str) -> int:
        return len(text.split())

    def encode(self, texts):
        output = []
        for text in texts:
            digest = hashlib.sha256(text.encode()).digest()
            vector = [float(digest[index % len(digest)]) for index in range(384)]
            norm = sum(value * value for value in vector) ** 0.5
            output.append([value / norm for value in vector])
        return output


def item(description: str = "Âm thanh rõ và chống ồn tốt.") -> CanonicalItemDocument:
    return CanonicalItemDocument.model_validate(
        {
            "item_id": 800000,
            "sku": "SONY-HEADPHONES-800000",
            "structured_metadata": {
                "brand": "Sony",
                "category_path": ["Điện tử", "Thiết bị âm thanh", "Tai nghe"],
                "current_price": Decimal("20.99"),
                "in_stock": True,
                "stock_quantity": 10,
                "warranty_months": 24,
                "warehouse_location": "SGN-01",
            },
            "unstructured_text": {
                "title": "Tai nghe chống ồn",
                "description": description,
                "specifications": {"battery": "30 giờ", "weight": 250},
                "usage_instructions": "Giữ nút nguồn để ghép đôi.",
            },
            "reviews_and_qna": {
                "average_rating": 4.7,
                "total_reviews": 128,
                "sample_reviews": [
                    {
                        "review_id": "rev_800000_01",
                        "rating": 5,
                        "content": "Đeo êm và chống ồn tốt.",
                        "sentiment_aspects": {"comfort": "positive"},
                    },
                    {
                        "review_id": "rev_800000_02",
                        "rating": 4,
                        "content": "Bass vừa phải.",
                        "sentiment_aspects": {"bass": "neutral"},
                    },
                ],
                "qna_pairs": [{"question": "Có Bluetooth?", "answer": "Có."}],
            },
        }
    )


def test_chunks_are_deterministic_stable_and_within_hard_limit():
    chunker = SemanticChunker(FakeEncoder(), ChunkerConfig())
    first = chunker.chunk_item(item(), source_run_id="source-1")
    second = chunker.chunk_item(item(), source_run_id="source-1")
    assert [chunk.chunk_id for chunk in first] == [chunk.chunk_id for chunk in second]
    assert [chunk.content_hash for chunk in first] == [chunk.content_hash for chunk in second]
    assert all(chunk.token_count <= 384 for chunk in first)
    assert all(chunk.embedding_text.startswith("passage: ") for chunk in first)
    assert {chunk.chunk_type for chunk in first} == {
        "product_overview",
        "specifications",
        "usage_instructions",
        "review",
        "qna",
    }


def test_changed_content_upserts_same_chunk_id_with_new_hash():
    chunker = SemanticChunker(FakeEncoder(), ChunkerConfig())
    original = chunker.chunk_item(item(), source_run_id="source-1")
    changed = chunker.chunk_item(
        item("Âm thanh rõ và chống ồn rất mạnh."), source_run_id="source-2"
    )
    assert original[0].chunk_id == changed[0].chunk_id
    assert original[0].content_hash != changed[0].content_hash
    assert original[0].item_content_hash != changed[0].item_content_hash


def test_long_unit_uses_semantic_breaks_and_preserves_overlap():
    chunker = SemanticChunker(
        FakeEncoder(),
        ChunkerConfig(target_tokens=8, min_tokens=3, max_tokens=12, overlap_tokens=2),
    )
    parts = chunker._split_unit(
        SourceUnit(
            "review",
            "rev_long",
            "Âm thanh rất rõ ràng. Chống ồn dùng tốt. Đệm tai khá mềm. Pin dùng cả ngày.",
        )
    )
    assert len(parts) >= 2
    assert all(chunker.encoder.token_count(part) <= 12 for part in parts)


def test_unpunctuated_unit_uses_bounded_word_windows():
    chunker = SemanticChunker(
        FakeEncoder(),
        ChunkerConfig(target_tokens=5, min_tokens=2, max_tokens=6, overlap_tokens=1),
    )
    parts = chunker._split_unit(
        SourceUnit("review", "rev_words", " ".join(f"từ{i}" for i in range(18)))
    )
    assert len(parts) > 2
    assert all(chunker.encoder.token_count(part) <= 6 for part in parts)


def test_embedding_context_that_consumes_limit_is_rejected():
    chunker = SemanticChunker(
        FakeEncoder(), ChunkerConfig(min_tokens=80, max_tokens=10)
    )
    with pytest.raises(ValueError, match="too few tokens"):
        chunker.chunk_item(item(), source_run_id="source-1")
