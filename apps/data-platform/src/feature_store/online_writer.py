from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class RedisKeyTemplate:
    user_sequence: str = "fs:user_sequence:{user_id}"
    user_aggregate: str = "fs:user_aggregate:{user_id}"
    item_features: str = "fs:item:{product_id}"


def json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def dumps_feature_payload(payload: dict[str, Any]) -> str:
    return json.dumps(json_safe(payload), allow_nan=False, default=str, sort_keys=True)


class RedisOnlineWriter:
    _WRITE_LATEST_SCRIPT = """
local current = redis.call('GET', KEYS[1])
if current then
  local ok, decoded = pcall(cjson.decode, current)
  if ok and decoded['updated_at'] and decoded['updated_at'] > ARGV[1] then
    return 0
  end
end
redis.call('SET', KEYS[1], ARGV[2], 'EX', ARGV[3])
return 1
"""

    def __init__(self, redis_client: Any, keys: RedisKeyTemplate | None = None):
        self.redis_client = redis_client
        self.keys = keys or RedisKeyTemplate()

    def _write_latest(self, key: str, payload: dict[str, Any], ttl_seconds: int) -> str:
        rendered = dumps_feature_payload(payload)
        updated_at = str(payload.get("updated_at") or "")
        if updated_at and hasattr(self.redis_client, "eval"):
            self.redis_client.eval(self._WRITE_LATEST_SCRIPT, 1, key, updated_at, rendered, int(ttl_seconds))
        else:
            self.redis_client.set(key, rendered, ex=ttl_seconds)
        return key

    def write_user_sequence(self, user_id: int, payload: dict[str, Any], ttl_seconds: int) -> str:
        key = self.keys.user_sequence.format(user_id=user_id)
        return self._write_latest(key, payload, ttl_seconds)

    def write_user_aggregate(self, user_id: int, payload: dict[str, Any], ttl_seconds: int) -> str:
        key = self.keys.user_aggregate.format(user_id=user_id)
        return self._write_latest(key, payload, ttl_seconds)

    def write_item_features(self, product_id: int, payload: dict[str, Any], ttl_seconds: int) -> str:
        key = self.keys.item_features.format(product_id=product_id)
        return self._write_latest(key, payload, ttl_seconds)
