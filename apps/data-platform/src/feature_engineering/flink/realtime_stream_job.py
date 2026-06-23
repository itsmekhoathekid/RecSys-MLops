from __future__ import annotations

import argparse
import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import pandas as pd

from feature_engineering.flink.candidate_pool_job import (
    candidate_updates,
)
from feature_engineering.flink.item_features_job import (
    ItemFeatureState,
)
from feature_engineering.flink.user_aggregate_job import (
    UserAggregateState,
)
from feature_engineering.flink.user_sequence_job import (
    UserSequenceState,
)
from feature_store.online_writer import RedisOnlineWriter
from ingest.bronze_cdc_reader import extract_debezium_after


EVENT_TYPE_IDS = {"view": 1, "cart": 2, "purchase": 3}


@dataclass
class StreamJobStats:
    consumed: int = 0
    skipped: int = 0
    duplicate: int = 0
    redis_writes: int = 0
    warehouse_writes: int = 0
    late_events: int = 0
    bursty_windows: int = 0


@dataclass
class StreamQualityWindow:
    window_start: pd.Timestamp
    window_end: pd.Timestamp
    topic: str
    event_count: int = 0
    late_event_count: int = 0
    max_late_by_seconds: float = 0.0
    is_bursty: bool = False

    def as_row(self) -> dict[str, Any]:
        return {
            "window_start": self.window_start.to_pydatetime(),
            "window_end": self.window_end.to_pydatetime(),
            "topic": self.topic,
            "event_count": self.event_count,
            "late_event_count": self.late_event_count,
            "max_late_by_seconds": self.max_late_by_seconds,
            "is_bursty": self.is_bursty,
            "created_timestamp": datetime.now(timezone.utc),
        }


class StreamQualityTracker:
    def __init__(
        self,
        topic: str,
        window_seconds: int = 60,
        burst_threshold_event_count: int = 500,
    ):
        self.topic = topic
        self.window_seconds = window_seconds
        self.burst_threshold_event_count = burst_threshold_event_count
        self.current: StreamQualityWindow | None = None

    def update(
        self,
        event_timestamp: str,
        late_by_seconds: float,
        is_late: bool,
    ) -> list[StreamQualityWindow]:
        ts = pd.Timestamp(event_timestamp)
        epoch = int(ts.timestamp())
        window_start_epoch = epoch - (epoch % self.window_seconds)
        window_start = pd.Timestamp(window_start_epoch, unit="s", tz="UTC")
        window_end = window_start + pd.Timedelta(seconds=self.window_seconds)
        emitted: list[StreamQualityWindow] = []
        if self.current is not None and self.current.window_start != window_start:
            self.current.is_bursty = self.current.event_count >= self.burst_threshold_event_count
            emitted.append(self.current)
            self.current = None
        if self.current is None:
            self.current = StreamQualityWindow(window_start, window_end, self.topic)
        self.current.event_count += 1
        self.current.late_event_count += 1 if is_late else 0
        self.current.max_late_by_seconds = max(self.current.max_late_by_seconds, late_by_seconds)
        return emitted

    def flush(self) -> list[StreamQualityWindow]:
        if self.current is None:
            return []
        self.current.is_bursty = self.current.event_count >= self.burst_threshold_event_count
        emitted = [self.current]
        self.current = None
        return emitted


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


def build_realtime_feature_payloads(
    event: dict[str, Any],
    sequence_state: UserSequenceState,
    aggregate_state: UserAggregateState,
    item_state: ItemFeatureState,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    sequence_payload = sequence_state.update(event)
    aggregate_payload = aggregate_state.update(event)
    item_payload = item_state.update(event)
    return sequence_payload, aggregate_payload, item_payload


def write_payloads_to_redis(
    event: dict[str, Any],
    writer: RedisOnlineWriter,
    redis_client: Any,
    sequence_payload: dict[str, Any],
    aggregate_payload: dict[str, Any],
    item_payload: dict[str, Any],
) -> int:
    writer.write_user_sequence(event["user_id"], sequence_payload, ttl_seconds=90 * 24 * 60 * 60)
    writer.write_user_aggregate(event["user_id"], aggregate_payload, ttl_seconds=24 * 60 * 60)
    writer.write_item_features(event["product_id"], item_payload, ttl_seconds=7 * 24 * 60 * 60)
    candidate_payloads = candidate_updates(item_payload)
    for key, product_id, score in candidate_payloads:
        redis_client.zadd(key, {str(product_id): float(score)})
    return 3 + len(candidate_payloads)


def write_event_to_redis(
    event: dict[str, Any],
    writer: RedisOnlineWriter,
    redis_client: Any,
    sequence_state: UserSequenceState,
    aggregate_state: UserAggregateState,
    item_state: ItemFeatureState,
) -> int:
    sequence_payload, aggregate_payload, item_payload = build_realtime_feature_payloads(
        event,
        sequence_state,
        aggregate_state,
        item_state,
    )
    return write_payloads_to_redis(
        event,
        writer,
        redis_client,
        sequence_payload,
        aggregate_payload,
        item_payload,
    )


def late_arrival_metrics(event: dict[str, Any], watermark_delay_minutes: int) -> tuple[float, bool]:
    processed_ts = pd.Timestamp.now(tz="UTC")
    event_ts = pd.Timestamp(event["event_timestamp"])
    late_by_seconds = max(0.0, float((processed_ts - event_ts).total_seconds()))
    return late_by_seconds, late_by_seconds > watermark_delay_minutes * 60


def build_warehouse_rows(
    event: dict[str, Any],
    sequence_payload: dict[str, Any],
    aggregate_payload: dict[str, Any],
    item_payload: dict[str, Any],
    source_topic: str,
    watermark_delay_minutes: int,
) -> dict[str, list[dict[str, Any]]]:
    late_by_seconds, is_late = late_arrival_metrics(event, watermark_delay_minutes)
    feature_ts = pd.Timestamp(event["event_timestamp"]).to_pydatetime()
    return {
        "stream_behavior_events": [
            {
                "event_id": str(event["event_id"]),
                "event_timestamp": feature_ts,
                "processed_timestamp": datetime.now(timezone.utc),
                "user_id": int(event["user_id"]),
                "product_id": int(event["product_id"]),
                "event_type": str(event["event_type"]),
                "event_type_id": int(event["event_type_id"]),
                "category_id": int(event["category_id"]),
                "brand_id": int(event["brand_id"]),
                "price": float(event["price"]),
                "price_bucket": int(event["price_bucket"]),
                "payload_hash": str(event.get("payload_hash") or ""),
                "source_topic": source_topic,
                "late_by_seconds": late_by_seconds,
                "is_late": is_late,
            }
        ],
        "stream_user_sequence_features": [
            {
                "user_id": int(sequence_payload["user_id"]),
                "feature_timestamp": feature_ts,
                "sequence_length": int(sequence_payload["sequence_length"]),
                "max_history_length": int(sequence_payload["max_history_length"]),
                "feature_payload": sequence_payload,
                "feature_version": sequence_payload["feature_version"],
            }
        ],
        "stream_user_aggregate_features": [
            {
                "user_id": int(aggregate_payload["user_id"]),
                "feature_timestamp": feature_ts,
                "views_30m": int(aggregate_payload["views_30m"]),
                "carts_30m": int(aggregate_payload["carts_30m"]),
                "purchases_24h": int(aggregate_payload["purchases_24h"]),
                "feature_payload": aggregate_payload,
                "feature_version": aggregate_payload["feature_version"],
            }
        ],
        "stream_item_features": [
            {
                "product_id": int(item_payload["product_id"]),
                "feature_timestamp": feature_ts,
                "category_id": int(item_payload["category_id"]),
                "brand_id": int(item_payload["brand_id"]),
                "price_bucket": int(item_payload["price_bucket"]),
                "views_1h": int(item_payload["views_1h"]),
                "views_24h": int(item_payload["views_24h"]),
                "purchases_24h": int(item_payload["purchases_24h"]),
                "popularity_score": float(item_payload["popularity_score"]),
                "feature_payload": item_payload,
                "feature_version": item_payload["feature_version"],
            }
        ],
    }


def write_warehouse_rows(connection: Any, rows: dict[str, list[dict[str, Any]]]) -> int:
    from warehouse.schemas import (
        STAGING_STREAM_BEHAVIOR_EVENTS,
        STAGING_STREAM_ITEM_FEATURES,
        STAGING_STREAM_USER_AGGREGATE_FEATURES,
        STAGING_STREAM_USER_SEQUENCE_FEATURES,
    )
    from warehouse.writer import upsert_rows

    return (
        upsert_rows(connection, STAGING_STREAM_BEHAVIOR_EVENTS, rows["stream_behavior_events"])
        + upsert_rows(connection, STAGING_STREAM_USER_SEQUENCE_FEATURES, rows["stream_user_sequence_features"])
        + upsert_rows(connection, STAGING_STREAM_USER_AGGREGATE_FEATURES, rows["stream_user_aggregate_features"])
        + upsert_rows(connection, STAGING_STREAM_ITEM_FEATURES, rows["stream_item_features"])
    )


def write_quality_windows(connection: Any, windows: list[StreamQualityWindow]) -> int:
    from warehouse.schemas import MONITORING_STREAMING_QUALITY_WINDOWS
    from warehouse.writer import upsert_rows

    return upsert_rows(
        connection,
        MONITORING_STREAMING_QUALITY_WINDOWS,
        [window.as_row() for window in windows],
    )


def maybe_init_pyflink() -> str:
    """Use PyFlink when available; local E2E can run bounded processing without it."""
    try:
        from pyflink.datastream import StreamExecutionEnvironment
    except ImportError as exc:
        return f"pyflink_unavailable={exc.__class__.__name__}"

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
    warehouse_connection = None
    quality_tracker = StreamQualityTracker(
        args.topic,
        window_seconds=args.quality_window_seconds,
        burst_threshold_event_count=args.burst_threshold_event_count,
    )
    if args.warehouse_enabled:
        from warehouse.connection import connect
        from warehouse.writer import ensure_warehouse

        warehouse_connection = connect()
        ensure_warehouse(warehouse_connection)

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
                    sequence_payload, aggregate_payload, item_payload = build_realtime_feature_payloads(
                        event,
                        sequence_state,
                        aggregate_state,
                        item_state,
                    )
                    stats.redis_writes += write_payloads_to_redis(
                        event,
                        writer,
                        redis_client,
                        sequence_payload,
                        aggregate_payload,
                        item_payload,
                    )
                    late_by_seconds, is_late = late_arrival_metrics(event, args.watermark_delay_minutes)
                    stats.late_events += 1 if is_late else 0
                    emitted_windows = quality_tracker.update(event["event_timestamp"], late_by_seconds, is_late)
                    stats.bursty_windows += sum(1 for window in emitted_windows if window.is_bursty)
                    if warehouse_connection is not None:
                        rows = build_warehouse_rows(
                            event,
                            sequence_payload,
                            aggregate_payload,
                            item_payload,
                            args.topic,
                            args.watermark_delay_minutes,
                        )
                        stats.warehouse_writes += write_warehouse_rows(warehouse_connection, rows)
                        stats.warehouse_writes += write_quality_windows(warehouse_connection, emitted_windows)
                    stats.consumed += 1
                    if not continuous and stats.consumed >= args.max_events:
                        break
                if not continuous and stats.consumed >= args.max_events:
                    break
    finally:
        consumer.close()
        final_windows = quality_tracker.flush()
        stats.bursty_windows += sum(1 for window in final_windows if window.is_bursty)
        if warehouse_connection is not None:
            stats.warehouse_writes += write_quality_windows(warehouse_connection, final_windows)
            warehouse_connection.close()

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
    parser.add_argument("--warehouse-enabled", action="store_true", default=os.getenv("WAREHOUSE_ENABLED", "").lower() in {"1", "true", "yes"})
    parser.add_argument("--watermark-delay-minutes", type=int, default=int(os.getenv("STREAM_WATERMARK_DELAY_MINUTES", "60")))
    parser.add_argument("--quality-window-seconds", type=int, default=int(os.getenv("STREAM_QUALITY_WINDOW_SECONDS", "60")))
    parser.add_argument("--burst-threshold-event-count", type=int, default=int(os.getenv("STREAM_BURST_THRESHOLD_EVENT_COUNT", "500")))
    args = parser.parse_args()

    stats = run_bounded_stream(args)
    print(json.dumps(stats.__dict__, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
