from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class RedisKeyTemplate:
    user_sequence: str = "fs:user_sequence:{user_id}"
    user_aggregate: str = "fs:user_aggregate:{user_id}"
    item_features: str = "fs:item:{product_id}"


def dumps_feature_payload(payload: dict[str, Any]) -> str:
    return json.dumps(payload, default=str, sort_keys=True)


class RedisOnlineWriter:
    def __init__(self, redis_client: Any, keys: RedisKeyTemplate | None = None):
        self.redis_client = redis_client
        self.keys = keys or RedisKeyTemplate()

    def write_user_sequence(self, user_id: int, payload: dict[str, Any], ttl_seconds: int) -> str:
        key = self.keys.user_sequence.format(user_id=user_id)
        self.redis_client.set(key, dumps_feature_payload(payload), ex=ttl_seconds)
        return key

    def write_user_aggregate(self, user_id: int, payload: dict[str, Any], ttl_seconds: int) -> str:
        key = self.keys.user_aggregate.format(user_id=user_id)
        self.redis_client.set(key, dumps_feature_payload(payload), ex=ttl_seconds)
        return key

    def write_item_features(self, product_id: int, payload: dict[str, Any], ttl_seconds: int) -> str:
        key = self.keys.item_features.format(product_id=product_id)
        self.redis_client.set(key, dumps_feature_payload(payload), ex=ttl_seconds)
        return key

