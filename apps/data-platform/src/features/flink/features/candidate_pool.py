from __future__ import annotations

from typing import Any


USER_CANDIDATE_LIMIT = 100
USER_CANDIDATE_TTL_SECONDS = 7 * 24 * 60 * 60


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
        (
            "candidate:popular:global",
            product_id,
            float(item_payload.get("popularity_score", score)),
        ),
        (
            f"candidate:popular:category:{category_id}",
            product_id,
            float(item_payload.get("popularity_score", score)),
        ),
    ]


def refresh_user_candidate_pool(
    redis_client: Any,
    *,
    user_id: int,
    category_id: int,
    limit: int = USER_CANDIDATE_LIMIT,
    ttl_seconds: int = USER_CANDIDATE_TTL_SECONDS,
) -> int:
    """Merge popular products from an interacted category into a user candidate pool."""
    if limit <= 0:
        return 0
    category_key = f"candidate:popular:category:{int(category_id)}"
    candidates = redis_client.zrevrange(category_key, 0, limit - 1, withscores=True)
    if not candidates:
        return 0

    scored_candidates = {
        str(
            product_id.decode("utf-8") if isinstance(product_id, bytes) else product_id
        ): float(score)
        for product_id, score in candidates
    }
    user_key = f"candidate:user:{int(user_id)}"
    redis_client.zadd(user_key, scored_candidates)
    redis_client.zremrangebyrank(user_key, 0, -limit - 1)
    redis_client.expire(user_key, int(ttl_seconds))
    return len(scored_candidates)
