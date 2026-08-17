from __future__ import annotations

import pytest
from pydantic import ValidationError

from rag_data.contracts import CanonicalReview, GeneratedItemContent


def valid_payload() -> dict:
    return {
        "title": "Tai nghe synthetic",
        "description": "Mô tả synthetic cho demo.",
        "specifications": {"battery_life": "30 giờ"},
        "usage_instructions": "Sạc trước khi sử dụng.",
        "reviews": [
            {"content": "Âm thanh tốt.", "sentiment_aspects": {"sound": "positive"}},
            {"content": "Đeo khá êm.", "sentiment_aspects": {"comfort": "positive"}},
        ],
        "qna_pairs": [{"question": "Có Bluetooth?", "answer": "Có."}],
    }


def test_generated_content_accepts_exact_contract():
    content = GeneratedItemContent.model_validate(valid_payload())
    assert len(content.reviews) == 2
    assert len(content.qna_pairs) == 1


@pytest.mark.parametrize(
    ("mutator", "expected"),
    [
        (lambda value: value.pop("title"), "title"),
        (lambda value: value.update(specifications={}), "specifications"),
        (lambda value: value.update(reviews=value["reviews"][:1]), "reviews"),
        (lambda value: value.update(qna_pairs=[]), "qna_pairs"),
        (lambda value: value.update(unexpected=True), "unexpected"),
    ],
)
def test_generated_content_rejects_invalid_shapes(mutator, expected):
    payload = valid_payload()
    mutator(payload)
    with pytest.raises(ValidationError, match=expected):
        GeneratedItemContent.model_validate(payload)


def test_generated_content_rejects_unsupported_sentiment():
    payload = valid_payload()
    payload["reviews"][0]["sentiment_aspects"]["sound"] = "excellent"
    with pytest.raises(ValidationError, match="sentiment_aspects"):
        GeneratedItemContent.model_validate(payload)


@pytest.mark.parametrize("rating", [0, 6])
def test_canonical_review_rejects_rating_outside_one_to_five(rating):
    with pytest.raises(ValidationError, match="rating"):
        CanonicalReview(
            review_id="rev_1_01",
            rating=rating,
            content="Synthetic review",
            sentiment_aspects={"sound": "positive"},
        )
