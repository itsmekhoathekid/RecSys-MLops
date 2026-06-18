from __future__ import annotations

from typing import Any


def trending_score(payload: dict[str, Any]) -> float:
    return (
        float(payload.get("views_1h", 0))
        + 3.0 * float(payload.get("carts_1h", 0))
        + 10.0 * float(payload.get("purchases_24h", 0))
    )


def candidate_updates(item_payload: dict[str, Any]) -> list[tuple[str, int, float]]:
    product_id = int(item_payload["product_id"])
    category_id = int(item_payload["category_id"])
    score = trending_score(item_payload)
    return [
        ("candidate:trending:1h", product_id, score),
        (f"candidate:trending:category:{category_id}", product_id, score),
        ("candidate:popular:global", product_id, float(item_payload.get("popularity_score", score))),
        (
            f"candidate:popular:category:{category_id}",
            product_id,
            float(item_payload.get("popularity_score", score)),
        ),
    ]

