from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from feature_store.online_writer import RedisOnlineWriter, dumps_feature_payload
from features.flink.features.candidate_pool import candidate_updates
from features.flink.pyflink_compat import AsyncFunction
from features.flink.sinks import emit_progress
from features.flink.sinks.rate_limit import AsyncTokenBucketRateLimiter


class AsyncRedisFeatureWriter(AsyncFunction):
    def __init__(self, args: Any) -> None:
        self.args = args

    def open(self, runtime_context):
        import redis.asyncio as redis

        self.redis_client = redis.Redis(
            host=self.args.redis_host,
            port=self.args.redis_port,
            decode_responses=True,
        )
        self.writer = RedisOnlineWriter(self.redis_client)
        self.rate_limiter = AsyncTokenBucketRateLimiter(
            self.args.redis_sink_max_events_per_second,
            self.args.sink_rate_limit_burst_events,
        )
        self.writes = 0
        self.rate_limit_wait_seconds = 0.0
        self.last_write_unixtime = 0

    async def async_invoke(self, update: dict[str, Any]) -> list[dict[str, Any]]:
        event = update["event"]
        self.rate_limit_wait_seconds += await self.rate_limiter.acquire()
        if update["kind"] == "user":
            feature_writes = (
                (
                    self.writer.keys.user_sequence.format(user_id=event["user_id"]),
                    update["sequence_payload"],
                    90 * 24 * 60 * 60,
                ),
                (
                    self.writer.keys.user_aggregate.format(user_id=event["user_id"]),
                    update["aggregate_payload"],
                    24 * 60 * 60,
                ),
            )
        else:
            feature_writes = (
                (
                    self.writer.keys.item_features.format(
                        product_id=event["product_id"]
                    ),
                    update["item_payload"],
                    7 * 24 * 60 * 60,
                ),
            )

        import asyncio

        await asyncio.gather(
            *(
                self.redis_client.eval(
                    self.writer._WRITE_LATEST_SCRIPT,
                    1,
                    key,
                    str(payload.get("updated_at") or ""),
                    dumps_feature_payload(payload),
                    ttl_seconds,
                )
                for key, payload, ttl_seconds in feature_writes
            )
        )
        candidate_payloads = []
        personalized_candidates = 0
        if update["kind"] == "item":
            item_payload = update["item_payload"]
            candidate_payloads = candidate_updates(item_payload)
            await asyncio.gather(
                *(
                    self.redis_client.zadd(key, {str(product_id): float(score)})
                    for key, product_id, score in candidate_payloads
                )
            )
            category_key = (
                f"candidate:popular:category:{int(item_payload['category_id'])}"
            )
            candidates = await self.redis_client.zrevrange(
                category_key, 0, 99, withscores=True
            )
            if candidates:
                scored_candidates = {
                    str(product_id): float(score) for product_id, score in candidates
                }
                user_key = f"candidate:user:{int(event['user_id'])}"
                await asyncio.gather(
                    self.redis_client.zadd(user_key, scored_candidates),
                    self.redis_client.zremrangebyrank(user_key, 0, -101),
                    self.redis_client.expire(user_key, 7 * 24 * 60 * 60),
                )
                personalized_candidates = len(scored_candidates)
        writes = (
            len(feature_writes)
            + len(candidate_payloads)
            + int(personalized_candidates > 0)
        )
        self.writes += writes
        if writes:
            self.last_write_unixtime = int(datetime.now(timezone.utc).timestamp())
        if (
            self.args.progress_log_events > 0
            and self.writes % self.args.progress_log_events == 0
        ):
            emit_progress(
                {
                    "status": "running",
                    "topic": self.args.topic,
                    "redis_writes": self.writes,
                }
            )
        return [update]

    def timeout(self, update: dict[str, Any]) -> list[dict[str, Any]]:
        event = update.get("event") or {}
        emit_progress(
            {
                "status": "redis_async_timeout",
                "topic": self.args.topic,
                "event_id": event.get("event_id"),
            }
        )
        return [update]


def async_redis_feature_writer(args: Any) -> AsyncRedisFeatureWriter:
    return AsyncRedisFeatureWriter(args)
