from __future__ import annotations

import json
import os
import shutil
import threading
import time
from pathlib import Path
from typing import Any

from observability import METRICS, observe_redis, span
from api_schemas import OnlineFeaturesResponse

USER_SEQUENCE_FEATURE_REFS = [
    "user_sequence_features:hist_item_ids",
    "user_sequence_features:hist_event_type_ids",
    "user_sequence_features:hist_category_ids",
    "user_sequence_features:hist_brand_ids",
    "user_sequence_features:hist_price_bucket_ids",
    "user_sequence_features:hist_event_timestamps",
    "user_sequence_features:hist_request_ids",
    "user_sequence_features:hist_impression_ids",
    "user_sequence_features:hist_length",
    "user_sequence_features:max_history_length",
    "user_sequence_features:feature_version",
    "user_aggregate_features:views_30m",
    "user_aggregate_features:carts_30m",
    "user_aggregate_features:purchases_24h",
    "user_aggregate_features:distinct_categories_7d",
    "user_aggregate_features:avg_viewed_price_7d",
    "user_aggregate_features:cart_to_purchase_ratio_7d",
    "user_aggregate_features:last_event_age_seconds",
]

ITEM_FEATURE_REFS = [
    "item_features:category_id",
    "item_features:brand_id",
    "item_features:price_bucket",
    "item_features:is_active",
    "item_features:views_1h",
    "item_features:views_24h",
    "item_features:carts_1h",
    "item_features:carts_24h",
    "item_features:purchases_24h",
    "item_features:purchases_7d",
    "item_features:conversion_rate_7d",
    "item_features:popularity_score",
    "item_features:feature_version",
]


def parse_json_bytes(value: bytes | str | None) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, bytes):
        value = value.decode("utf-8")
    if not value:
        return {}
    return json.loads(value)


def normalize_feature_value(value: Any) -> Any:
    if hasattr(value, "tolist"):
        return value.tolist()
    if isinstance(value, tuple):
        return list(value)
    return value


def first_feature_row(features: dict[str, list[Any]], entity_keys: set[str]) -> dict[str, Any]:
    row: dict[str, Any] = {}
    for name, values in features.items():
        if name in entity_keys or not values:
            continue
        value = normalize_feature_value(values[0])
        if value is None:
            continue
        row[name] = value
    return row


class FeatureClient:
    def __init__(self, allow_fallback: bool | None = None) -> None:
        import redis

        if allow_fallback is None:
            allow_fallback = os.getenv("ALLOW_FEATURE_FALLBACK", "0") == "1"
        self.allow_fallback = allow_fallback
        self.feast_repo_path = Path(
            os.getenv("FEAST_REPO_PATH", "/opt/recsys/apps/data-platform/feature-store/feature_repo")
        )
        self.feast_runtime_repo_path = Path(os.getenv("FEAST_RUNTIME_REPO_PATH", "/tmp/recsys-feast-feature-repo"))
        self.feast_apply_on_startup = os.getenv("FEAST_APPLY_ON_STARTUP", "1") == "1"
        self._store: Any | None = None
        self._store_lock = threading.RLock()
        self.client = redis.Redis(
            host=os.getenv("REDIS_HOST", "localhost"),
            port=int(os.getenv("REDIS_PORT", "6379")),
            db=int(os.getenv("REDIS_DB", "0")),
        )

    def _redis_connection_string(self) -> str:
        return os.getenv(
            "FEAST_REDIS_CONNECTION_STRING",
            f"{os.getenv('REDIS_HOST', 'localhost')}:{int(os.getenv('REDIS_PORT', '6379'))}",
        )

    def _prepare_runtime_repo(self) -> Path:
        source = self.feast_repo_path
        target = self.feast_runtime_repo_path
        if target.exists():
            shutil.rmtree(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, target)
        (target / "data").mkdir(parents=True, exist_ok=True)
        config_path = target / "feature_store.yaml"
        config = config_path.read_text(encoding="utf-8")
        config = config.replace("connection_string: redis:6379", f"connection_string: {self._redis_connection_string()}")
        config_path.write_text(config, encoding="utf-8")
        return target

    def _feature_store(self):
        if self._store is None:
            with self._store_lock:
                if self._store is None:
                    from feast import FeatureStore
                    from feature_store.feast_registry import apply_feature_repo

                    repo_path = self._prepare_runtime_repo()
                    if self.feast_apply_on_startup:
                        apply_feature_repo(repo_path, skip_source_validation=True)
                    self._store = FeatureStore(repo_path=str(repo_path))
        return self._store

    def _get_feast_online_features(self, features: list[str], entity_rows: list[dict[str, Any]]) -> dict[str, list[Any]]:
        with self._store_lock:
            return self._feature_store().get_online_features(features=features, entity_rows=entity_rows).to_dict()

    def user_sequence(self, user_id: int) -> dict[str, Any]:
        start = time.perf_counter()
        try:
            with span("feast.user_features", operation="user_features"):
                payload = first_feature_row(
                    self._get_feast_online_features(USER_SEQUENCE_FEATURE_REFS, [{"user_id": user_id}]),
                    {"user_id"},
                )
            observe_redis("user_sequence", time.perf_counter() - start)
            if not payload:
                METRICS.inc("recsys_api_empty_feature_total", labels={"feature": "user_sequence"})
            return payload
        except Exception as exc:
            observe_redis("user_sequence", time.perf_counter() - start, error=True)
            if self.allow_fallback:
                return {}
            raise RuntimeError(f"failed to fetch user features from Feast online store for user_id={user_id}") from exc

    def item_features(self, item_id: int) -> dict[str, Any]:
        return self.item_features_batch([item_id]).get(str(item_id), {})

    def item_features_batch(self, item_ids: list[int]) -> dict[str, dict[str, Any]]:
        start = time.perf_counter()
        try:
            entity_rows = [{"product_id": item_id} for item_id in item_ids]
            with span("feast.item_features", operation="item_features", item_count=len(item_ids)):
                features = self._get_feast_online_features(ITEM_FEATURE_REFS, entity_rows)
            rows: dict[str, dict[str, Any]] = {}
            for index, item_id in enumerate(item_ids):
                row = {}
                for name, values in features.items():
                    if name == "product_id" or index >= len(values):
                        continue
                    value = normalize_feature_value(values[index])
                    if value is not None:
                        row[name] = value
                rows[str(item_id)] = row
            observe_redis("item_features", time.perf_counter() - start)
            if not rows:
                METRICS.inc("recsys_api_empty_feature_total", labels={"feature": "item_features"})
            return rows
        except Exception as exc:
            observe_redis("item_features", time.perf_counter() - start, error=True)
            if self.allow_fallback:
                return {str(item_id): {} for item_id in item_ids}
            raise RuntimeError("failed to fetch item features from Feast online store") from exc

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
    if hasattr(feature_client, "item_features_batch"):
        item_rows = feature_client.item_features_batch(candidates)
    else:
        item_rows = {str(item_id): feature_client.item_features(item_id) for item_id in candidates}
    return OnlineFeaturesResponse(
        user_id=user_id,
        candidate_item_ids=candidates,
        user_sequence=feature_client.user_sequence(user_id),
        item_features=item_rows,
    )
