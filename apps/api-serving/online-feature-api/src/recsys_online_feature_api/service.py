from __future__ import annotations

import asyncio
import inspect
import json
import shutil
import threading
import time
from pathlib import Path
from typing import Any

from recsys_serving_common.concurrency import BoundedExecutor, CapacityExceeded
from recsys_serving_common.contracts import OnlineFeaturesResponse
from recsys_serving_common.observability import METRICS, observe_redis, span
from recsys_online_feature_api.settings import FeatureApiSettings

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

REALTIME_SEQUENCE_FIELDS = {
    "item_ids": "hist_item_ids",
    "event_type_ids": "hist_event_type_ids",
    "category_ids": "hist_category_ids",
    "brand_ids": "hist_brand_ids",
    "price_bucket_ids": "hist_price_bucket_ids",
    "event_timestamps": "hist_event_timestamps",
    "request_ids": "hist_request_ids",
    "impression_ids": "hist_impression_ids",
    "sequence_length": "hist_length",
    "max_history_length": "max_history_length",
    "feature_version": "feature_version",
}
REALTIME_AGGREGATE_FIELDS = {
    "views_30m",
    "carts_30m",
    "purchases_24h",
    "distinct_categories_7d",
    "avg_viewed_price_7d",
    "cart_to_purchase_ratio_7d",
    "last_event_age_seconds",
}


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


def first_feature_row(
    features: dict[str, list[Any]], entity_keys: set[str]
) -> dict[str, Any]:
    row: dict[str, Any] = {}
    for name, values in features.items():
        if name in entity_keys or not values:
            continue
        value = normalize_feature_value(values[0])
        if value is None:
            continue
        row[name] = value
    return row


def normalize_realtime_user_features(
    sequence: dict[str, Any], aggregate: dict[str, Any] | None = None
) -> dict[str, Any]:
    payload = {
        target: normalize_feature_value(sequence[source])
        for source, target in REALTIME_SEQUENCE_FIELDS.items()
        if source in sequence and sequence[source] is not None
    }
    if aggregate:
        payload.update(
            {
                name: normalize_feature_value(aggregate[name])
                for name in REALTIME_AGGREGATE_FIELDS
                if name in aggregate and aggregate[name] is not None
            }
        )
    return payload


class FeatureClient:
    def __init__(
        self,
        allow_fallback: bool | None = None,
        settings: FeatureApiSettings | None = None,
    ) -> None:
        import redis.asyncio as redis

        settings = settings or FeatureApiSettings.from_env()
        if allow_fallback is None:
            allow_fallback = settings.allow_fallback
        self.allow_fallback = allow_fallback
        self.feast_repo_path = Path(settings.feast_repo_path)
        self.feast_runtime_repo_path = Path(settings.feast_runtime_repo_path)
        self.feast_apply_on_startup = settings.feast_apply_on_startup
        self.feast_redis_connection_string = settings.feast_redis_connection_string
        self._store: Any | None = None
        self._store_lock = threading.RLock()
        self._feast_executor = BoundedExecutor(
            workers=settings.feast_workers,
            queue_size=settings.feast_queue_size,
            wait_seconds=settings.capacity_wait_seconds,
            operation="feast_online_features",
        )
        self.client = redis.Redis(
            host=settings.redis_host,
            port=settings.redis_port,
            db=settings.redis_db,
            max_connections=settings.redis_max_connections,
            socket_connect_timeout=settings.redis_timeout_seconds,
            socket_timeout=settings.redis_timeout_seconds,
        )

    async def warmup(self) -> None:
        await self._feast_executor.run(self._feature_store)

    async def aclose(self) -> None:
        await self._feast_executor.aclose()
        close = getattr(self.client, "aclose", None) or getattr(
            self.client, "close", None
        )
        if close is not None:
            result = close()
            if inspect.isawaitable(result):
                await result

    def _redis_connection_string(self) -> str:
        return self.feast_redis_connection_string

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
        config = config.replace(
            "connection_string: redis:6379",
            f"connection_string: {self._redis_connection_string()}",
        )
        config_path.write_text(config, encoding="utf-8")
        return target

    def _feature_store(self):
        if self._store is None:
            with self._store_lock:
                if self._store is None:
                    from feast import FeatureStore
                    from recsys_feature_store_runtime.feast_registry import (
                        apply_feature_repo,
                    )
                    from recsys_feature_store_runtime.sql_registry_state import (
                        configure_registry_url,
                    )

                    configure_registry_url()
                    repo_path = self._prepare_runtime_repo()
                    if self.feast_apply_on_startup:
                        apply_feature_repo(repo_path, skip_source_validation=True)
                    self._store = FeatureStore(repo_path=str(repo_path))
        return self._store

    def _get_feast_online_features(
        self, features: list[str], entity_rows: list[dict[str, Any]]
    ) -> dict[str, list[Any]]:
        with self._store_lock:
            return (
                self._feature_store()
                .get_online_features(features=features, entity_rows=entity_rows)
                .to_dict()
            )

    def _feast_user_sequence(self, user_id: int) -> dict[str, Any]:
        return first_feature_row(
            self._get_feast_online_features(
                USER_SEQUENCE_FEATURE_REFS, [{"user_id": user_id}]
            ),
            {"user_id"},
        )

    async def user_sequence(self, user_id: int) -> dict[str, Any]:
        start = time.perf_counter()
        try:
            with span("redis.realtime_user_features", operation="user_features"):
                try:
                    values = await self.client.mget(
                        [
                            f"fs:user_sequence:{user_id}",
                            f"fs:user_aggregate:{user_id}",
                        ]
                    )
                    realtime_sequence = parse_json_bytes(values[0])
                    realtime_aggregate = parse_json_bytes(values[1])
                except Exception:
                    realtime_sequence = {}
                    realtime_aggregate = {}
            if realtime_sequence:
                payload = normalize_realtime_user_features(
                    realtime_sequence, realtime_aggregate
                )
            else:
                with span("feast.user_features", operation="user_features"):
                    payload = await self._feast_executor.run(
                        self._feast_user_sequence, user_id
                    )
            observe_redis("user_sequence", time.perf_counter() - start)
            if not payload:
                METRICS.inc(
                    "recsys_api_empty_feature_total",
                    labels={"feature": "user_sequence"},
                )
            return payload
        except CapacityExceeded:
            raise
        except Exception as exc:
            observe_redis("user_sequence", time.perf_counter() - start, error=True)
            if self.allow_fallback:
                return {}
            raise RuntimeError(
                f"failed to fetch user features from Feast online store for user_id={user_id}"
            ) from exc

    async def item_features(self, item_id: int) -> dict[str, Any]:
        return (await self.item_features_batch([item_id])).get(str(item_id), {})

    def _item_features_batch_sync(
        self, item_ids: list[int]
    ) -> dict[str, dict[str, Any]]:
        entity_rows = [{"product_id": item_id} for item_id in item_ids]
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
        return rows

    async def item_features_batch(
        self, item_ids: list[int]
    ) -> dict[str, dict[str, Any]]:
        start = time.perf_counter()
        try:
            with span(
                "feast.item_features",
                operation="item_features",
                item_count=len(item_ids),
            ):
                rows = await self._feast_executor.run(
                    self._item_features_batch_sync, item_ids
                )
            observe_redis("item_features", time.perf_counter() - start)
            if not rows:
                METRICS.inc(
                    "recsys_api_empty_feature_total",
                    labels={"feature": "item_features"},
                )
            return rows
        except CapacityExceeded:
            raise
        except Exception as exc:
            observe_redis("item_features", time.perf_counter() - start, error=True)
            if self.allow_fallback:
                return {str(item_id): {} for item_id in item_ids}
            raise RuntimeError(
                "failed to fetch item features from Feast online store"
            ) from exc

    async def candidates(self, user_id: int, limit: int) -> list[int]:
        start = time.perf_counter()
        try:
            with span(
                "redis.candidates", operation="candidates", user_id=user_id, limit=limit
            ):
                personalized = await self.client.zrevrange(
                    f"candidate:user:{user_id}",
                    0,
                    max(limit - 1, 0),
                )
                global_candidates = (
                    await self.client.zrevrange(
                        "candidate:popular:global", 0, max(limit - 1, 0)
                    )
                    if not personalized
                    else []
                )
            candidates: list[int] = []
            seen: set[int] = set()
            for item in [*personalized, *global_candidates]:
                product_id = int(
                    item.decode("utf-8") if isinstance(item, bytes) else item
                )
                if product_id in seen:
                    continue
                seen.add(product_id)
                candidates.append(product_id)
                if len(candidates) >= limit:
                    break
            observe_redis("candidates", time.perf_counter() - start)
            if candidates or self.allow_fallback:
                return candidates
            raise RuntimeError(
                f"candidate:user:{user_id} and candidate:popular:global returned no candidates"
            )
        except Exception as exc:
            observe_redis("candidates", time.perf_counter() - start, error=True)
            if self.allow_fallback:
                return list(range(1, limit + 1))
            raise RuntimeError("failed to fetch candidate item IDs from Redis") from exc


async def _resolve(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


async def get_online_features(
    user_id: int,
    candidate_item_ids: list[int] | None,
    top_k: int,
    feature_client: FeatureClient,
) -> OnlineFeaturesResponse:
    candidates = candidate_item_ids or await _resolve(
        feature_client.candidates(user_id, max(top_k * 5, top_k))
    )
    if hasattr(feature_client, "item_features_batch"):
        item_operation = _resolve(feature_client.item_features_batch(candidates))
    else:

        async def fetch_items() -> dict[str, dict[str, Any]]:
            rows = await asyncio.gather(
                *(
                    _resolve(feature_client.item_features(item_id))
                    for item_id in candidates
                )
            )
            return {str(item_id): row for item_id, row in zip(candidates, rows)}

        item_operation = fetch_items()
    item_rows, user_sequence = await asyncio.gather(
        item_operation,
        _resolve(feature_client.user_sequence(user_id)),
    )
    return OnlineFeaturesResponse(
        user_id=user_id,
        candidate_item_ids=candidates,
        user_sequence=user_sequence,
        item_features=item_rows,
    )
