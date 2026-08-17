"""Versioned OrcaRouter prompts for synthetic Vietnamese catalog content.

Prompts contain grounding fields but no credentials. They explicitly forbid
model-generated IDs and filter metadata, leaving deterministic code in control.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any


PROMPT_VERSION = "rag_item_content_v1"

SYSTEM_PROMPT = """Bạn tạo nội dung catalog synthetic bằng tiếng Việt cho demo RAG.
Chỉ trả về một JSON object hợp lệ, không Markdown, không giải thích và không chain-of-thought.
Không trả item_id, SKU, giá, tồn kho, thương hiệu hay taxonomy ở top level.
Không khẳng định nội dung là thông tin sản phẩm đã được xác minh.
Output bắt buộc có đúng các key: title, description, specifications,
usage_instructions, reviews, qna_pairs. reviews có đúng 2 phần tử; qna_pairs có đúng 1.
Mỗi review chỉ có content và sentiment_aspects. Mỗi Q&A chỉ có question và answer.
sentiment_aspects là object không rỗng, value chỉ là positive, neutral hoặc negative.
specifications là object không rỗng với các giá trị scalar."""


def user_prompt(
    *,
    item_id: int,
    source_product_name: str,
    brand: str,
    category_path: list[str],
    current_price: Decimal,
) -> str:
    """Render one compact grounding prompt without changing the source price."""

    grounding: dict[str, Any] = {
        "source_item_id": item_id,
        "source_product_name": source_product_name,
        "mapped_brand": brand,
        "mapped_category_path": category_path,
        "source_current_price": str(current_price),
        "content_policy": "synthetic_demo_not_verified_product_facts",
    }
    return (
        "Hãy tạo nội dung catalog phù hợp với dữ liệu grounding sau. "
        "Không thêm field ngoài schema đã yêu cầu:\n"
        + json.dumps(grounding, ensure_ascii=False, sort_keys=True)
    )


def repair_prompt(validation_error: str) -> str:
    """Request a full corrected object after JSON or schema validation fails."""

    return (
        "Output trước không hợp lệ. Hãy trả lại toàn bộ JSON object đã sửa, không kèm "
        "giải thích. Lỗi validation: " + validation_error[:1200]
    )
