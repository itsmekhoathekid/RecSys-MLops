from __future__ import annotations

import argparse
import json
import os
import time
from dataclasses import dataclass
from typing import Any

import pandas as pd

from pipelines.data_pipeline.feature_engineering.flink.candidate_pool_job import (
    candidate_updates,
)
from pipelines.data_pipeline.feature_engineering.flink.item_features_job import (
    ItemFeatureState,
)
from pipelines.data_pipeline.feature_engineering.flink.user_aggregate_job import (
    UserAggregateState,
)
from pipelines.data_pipeline.feature_engineering.flink.user_sequence_job import (
    UserSequenceState,
)
from pipelines.data_pipeline.feature_store.online_writer import RedisOnlineWriter
from pipelines.data_pipeline.ingest.bronze_cdc_reader import extract_debezium_after


EVENT_TYPE_IDS = {"view": 1, "cart": 2, "purchase": 3}


@dataclass
class StreamJobStats:
    consumed: int = 0
    skipped: int = 0
    duplicate: int = 0
    redis_writes: int = 0


def safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def parse_message(raw: bytes | str | dict[str, Any]) -> dict[str, Any] | None:
    if isinstance(raw, bytes):
        record = json.loads(raw.decode("utf-8"))
    elif isinstance(raw, str):
        record = json.loads(raw)
    else:
        record = raw
    return extract_debezium_after(record)


def normalize_event(after: dict[str, Any]) -> dict[str, Any] | None:
    required = ["event_id", "user_id", "product_id", "event_type", "event_timestamp"]
    if any(after.get(column) in {None, ""} for column in required):
        return None

    event = dict(after)
    event_type = str(event["event_type"])
    event["event_type_id"] = safe_int(event.get("event_type_id"), EVENT_TYPE_IDS.get(event_type, 0))
    event["category_id"] = safe_int(event.get("category_id"))
    event["brand_id"] = safe_int(event.get("brand_id"))
    event["price_bucket"] = safe_int(event.get("price_bucket"))
    event["price"] = safe_float(event.get("price"), float(event["price_bucket"]))
    event["user_id"] = safe_int(event["user_id"])
    event["product_id"] = safe_int(event["product_id"])
    event["event_timestamp"] = pd.to_datetime(event["event_timestamp"], utc=True).isoformat()
    return event


def write_event_to_redis(
    event: dict[str, Any],
    writer: RedisOnlineWriter,
    redis_client: Any,
    sequence_state: UserSequenceState,
    aggregate_state: UserAggregateState,
    item_state: ItemFeatureState,
) -> int:
    sequence_payload = sequence_state.update(event)
    aggregate_payload = aggregate_state.update(event)
    item_payload = item_state.update(event)

    writer.write_user_sequence(event["user_id"], sequence_payload, ttl_seconds=90 * 24 * 60 * 60)
    writer.write_user_aggregate(event["user_id"], aggregate_payload, ttl_seconds=24 * 60 * 60)
    writer.write_item_features(event["product_id"], item_payload, ttl_seconds=7 * 24 * 60 * 60)
    candidate_payloads = candidate_updates(item_payload)
    for key, product_id, score in candidate_payloads:
        redis_client.zadd(key, {str(product_id): float(score)})
    return 3 + len(candidate_payloads)


def maybe_init_pyflink() -> str:
    """Import PyFlink in the Flink image so missing runtime deps fail early."""
    from pyflink.datastream import StreamExecutionEnvironment

    env = StreamExecutionEnvironment.get_execution_environment()
    env.set_parallelism(1)
    return f"pyflink_parallelism={env.get_parallelism()}"


def run_bounded_stream(args: argparse.Namespace) -> StreamJobStats:
    import redis
    from kafka import KafkaConsumer

    pyflink_status = maybe_init_pyflink()
    print(json.dumps({"status": pyflink_status, "topic": args.topic}))

    redis_client = redis.Redis(host=args.redis_host, port=args.redis_port, decode_responses=True)
    writer = RedisOnlineWriter(redis_client)
    sequence_state = UserSequenceState(max_history_length=args.max_history_length)
    aggregate_state = UserAggregateState()
    item_state = ItemFeatureState()
    seen_event_ids: set[str] = set()
    stats = StreamJobStats()

    consumer = KafkaConsumer(
        args.topic,
        bootstrap_servers=args.bootstrap_servers,
        group_id=args.group_id,
        auto_offset_reset="earliest",
        enable_auto_commit=True,
        consumer_timeout_ms=1000,
    )
    continuous = args.continuous or args.max_events <= 0
    deadline = None if continuous else time.monotonic() + args.idle_timeout_seconds
    try:
        while continuous or stats.consumed < args.max_events:
            records = consumer.poll(timeout_ms=1000, max_records=args.max_poll_records)
            if not records:
                if deadline is not None and time.monotonic() >= deadline:
                    break
                continue
            if not continuous:
                deadline = time.monotonic() + args.idle_timeout_seconds
            for batch in records.values():
                for message in batch:
                    after = parse_message(message.value)
                    if after is None:
                        stats.skipped += 1
                        continue
                    event = normalize_event(after)
                    if event is None:
                        stats.skipped += 1
                        continue
                    event_id = str(event["event_id"])
                    if event_id in seen_event_ids:
                        stats.duplicate += 1
                        continue
                    seen_event_ids.add(event_id)
                    stats.redis_writes += write_event_to_redis(
                        event,
                        writer,
                        redis_client,
                        sequence_state,
                        aggregate_state,
                        item_state,
                    )
                    stats.consumed += 1
                    if not continuous and stats.consumed >= args.max_events:
                        break
                if not continuous and stats.consumed >= args.max_events:
                    break
    finally:
        consumer.close()

    if stats.consumed < args.min_events:
        raise SystemExit(f"Only consumed {stats.consumed} events from {args.topic}; need {args.min_events}")
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description="Bounded local PyFlink realtime feature job.")
    parser.add_argument("--topic", default="cdc.behavior_events")
    parser.add_argument("--bootstrap-servers", default=os.getenv("KAFKA_BOOTSTRAP_SERVERS", "kafka:29092"))
    parser.add_argument("--redis-host", default=os.getenv("REDIS_HOST", "redis"))
    parser.add_argument("--redis-port", type=int, default=int(os.getenv("REDIS_PORT", "6379")))
    parser.add_argument("--group-id", default="recsys-flink-realtime-local")
    parser.add_argument("--max-events", type=int, default=200)
    parser.add_argument("--min-events", type=int, default=1)
    parser.add_argument("--continuous", action="store_true")
    parser.add_argument("--max-poll-records", type=int, default=100)
    parser.add_argument("--idle-timeout-seconds", type=int, default=60)
    parser.add_argument("--max-history-length", type=int, default=50)
    args = parser.parse_args()

    stats = run_bounded_stream(args)
    print(json.dumps(stats.__dict__, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
