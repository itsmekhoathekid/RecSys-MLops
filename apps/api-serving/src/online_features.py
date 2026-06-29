from __future__ import annotations

import json
import os
import time
from typing import Any

from observability import METRICS, observe_redis, span
from api_schemas import OnlineFeaturesResponse


def parse_json_bytes(value: bytes | str | None) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, bytes):
        value = value.decode("utf-8")
    if not value:
        return {}
    return json.loads(value)


class FeatureClient:
    def __init__(self, allow_fallback: bool | None = None) -> None:
        import redis

        if allow_fallback is None:
            allow_fallback = os.getenv("ALLOW_FEATURE_FALLBACK", "0") == "1"
        self.allow_fallback = allow_fallback
        self.client = redis.Redis(
            host=os.getenv("REDIS_HOST", "localhost"),
            port=int(os.getenv("REDIS_PORT", "6379")),
            db=int(os.getenv("REDIS_DB", "0")),
        )

    def user_sequence(self, user_id: int) -> dict[str, Any]:
        start = time.perf_counter()
        try:
            with span("redis.user_sequence", operation="user_sequence"):
                payload = parse_json_bytes(self.client.get(f"fs:user_sequence:{user_id}"))
            observe_redis("user_sequence", time.perf_counter() - start)
            if not payload:
                METRICS.inc("recsys_api_empty_feature_total", labels={"feature": "user_sequence"})
            return payload
        except Exception as exc:
            observe_redis("user_sequence", time.perf_counter() - start, error=True)
            if self.allow_fallback:
                return {}
            raise RuntimeError(f"failed to fetch user sequence from Redis for user_id={user_id}") from exc

    def item_features(self, item_id: int) -> dict[str, Any]:
        start = time.perf_counter()
        try:
            with span("redis.item_features", operation="item_features"):
                payload = parse_json_bytes(self.client.get(f"fs:item:{item_id}"))
            observe_redis("item_features", time.perf_counter() - start)
            if not payload:
                METRICS.inc("recsys_api_empty_feature_total", labels={"feature": "item_features"})
            return payload
        except Exception as exc:
            observe_redis("item_features", time.perf_counter() - start, error=True)
            if self.allow_fallback:
                return {}
            raise RuntimeError(f"failed to fetch item features from Redis for item_id={item_id}") from exc

    def candidates(self, user_id: int, limit: int) -> list[int]:
        start = time.perf_counter()
        try:
            with span("redis.candidates", operation="candidates", limit=limit):
                raw = self.client.zrevrange("candidate:popular:global", 0, max(limit - 1, 0))
            candidates = [int(item.decode("utf-8") if isinstance(item, bytes) else item) for item in raw]
            observe_redis("candidates", time.perf_counter() - start)
            if candidates or self.allow_fallback:
                return candidates
            raise RuntimeError("candidate:popular:global returned no candidates")
        except Exception as exc:
            observe_redis("candidates", time.perf_counter() - start, error=True)
            if self.allow_fallback:
                return list(range(1, limit + 1))
            raise RuntimeError("failed to fetch candidate item IDs from Redis") from exc


def get_online_features(
    user_id: int,
    candidate_item_ids: list[int] | None,
    top_k: int,
    feature_client: FeatureClient,
) -> OnlineFeaturesResponse:
    candidates = candidate_item_ids or feature_client.candidates(user_id, max(top_k * 5, top_k))
    item_rows = {str(item_id): feature_client.item_features(item_id) for item_id in candidates}
    return OnlineFeaturesResponse(
        user_id=user_id,
        candidate_item_ids=candidates,
        user_sequence=feature_client.user_sequence(user_id),
        item_features=item_rows,
    )
