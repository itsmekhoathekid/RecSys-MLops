from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from collections import deque
from typing import Any

from features.flink.candidate_pool_job import (
    candidate_updates,
    refresh_user_candidate_pool,
)
from features.flink.item_features_job import (
    ItemFeatureState,
)
from features.flink.user_aggregate_job import (
    UserAggregateState,
)
from features.flink.user_sequence_job import (
    UserSequenceState,
)
from features.flink.time_utils import isoformat_utc, parse_event_time
from feature_store.online_writer import RedisOnlineWriter, dumps_feature_payload
from feature_store.postgres_offline_store import (
    FEATURE_TABLES,
    PostgresOfflineStoreConfig,
    ensure_offline_store_tables,
    insert_offline_rows,
)
from ingest.debezium import extract_debezium_after
from metadata.governance_catalog import (
    ICEBERG_FEATURE_URNS,
    KAFKA_TOPIC_URNS,
    POSTGRES_FEATURE_URNS,
    REDIS_FEATURE_URNS,
)
from metadata.runtime_lineage import RuntimeLineageRecorder, lineage_run_id


EVENT_TYPE_IDS = {"view": 1, "cart": 2, "purchase": 3}


def stream_pipeline_role(args: argparse.Namespace) -> str:
    if args.disable_offline_store and not args.disable_online_store:
        return "online"
    if args.disable_online_store and args.offline_store_enabled:
        return "offline"
    if args.disable_online_store and not args.offline_store_enabled:
        return "disabled"
    return "hybrid"


def emit_progress(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, sort_keys=True) + "\n")
    sys.stdout.flush()


def flink_timestamp(value: Any) -> datetime:
    dt = parse_event_time(value) if isinstance(value, str) else value
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    microsecond = (dt.microsecond // 1000) * 1000
    return dt.replace(microsecond=microsecond)


@dataclass
class StreamJobStats:
    consumed: int = 0
    skipped: int = 0
    duplicate: int = 0
    redis_writes: int = 0
    offline_writes: int = 0
    late_events: int = 0
    bursty_windows: int = 0


@dataclass
class StreamQualityWindow:
    window_start: datetime
    window_end: datetime
    topic: str
    event_count: int = 0
    late_event_count: int = 0
    late_events_dropped: int = 0
    side_output_late_events: int = 0
    duplicate_event_count: int = 0
    max_late_by_seconds: float = 0.0
    is_bursty: bool = False

    def as_row(self) -> dict[str, Any]:
        return {
            "window_start": self.window_start,
            "window_end": self.window_end,
            "topic": self.topic,
            "event_count": self.event_count,
            "late_event_count": self.late_event_count,
            "late_events_dropped": self.late_events_dropped,
            "side_output_late_events": self.side_output_late_events,
            "duplicate_event_count": self.duplicate_event_count,
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
        is_duplicate: bool = False,
        drop_late_events: bool = False,
    ) -> list[StreamQualityWindow]:
        ts = parse_event_time(event_timestamp)
        event_unix_seconds = int(ts.timestamp())
        window_start_seconds = event_unix_seconds - (event_unix_seconds % self.window_seconds)
        window_start = datetime.fromtimestamp(window_start_seconds, tz=timezone.utc)
        window_end = window_start + timedelta(seconds=self.window_seconds)
        emitted: list[StreamQualityWindow] = []
        if self.current is not None and self.current.window_start != window_start:
            self.current.is_bursty = self.current.event_count >= self.burst_threshold_event_count
            emitted.append(self.current)
            self.current = None
        if self.current is None:
            self.current = StreamQualityWindow(window_start, window_end, self.topic)
        self.current.event_count += 1
        self.current.late_event_count += 1 if is_late else 0
        self.current.late_events_dropped += 1 if is_late and drop_late_events else 0
        self.current.side_output_late_events += 1 if is_late else 0
        self.current.duplicate_event_count += 1 if is_duplicate else 0
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


def env_int(name: str, default: int) -> int:
    return safe_int(os.getenv(name), default)


def env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.lower() in {"1", "true", "yes", "on"}


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
    event["event_timestamp"] = isoformat_utc(event["event_timestamp"])
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
    personalized_candidates = refresh_user_candidate_pool(
        redis_client,
        user_id=event["user_id"],
        category_id=item_payload["category_id"],
    )
    return 3 + len(candidate_payloads) + int(personalized_candidates > 0)


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


def late_arrival_metrics(event: dict[str, Any], allowed_lateness_seconds: int) -> tuple[float, bool]:
    processed_ts = datetime.now(timezone.utc)
    event_ts = parse_event_time(event["event_timestamp"])
    late_by_seconds = max(0.0, float((processed_ts - event_ts).total_seconds()))
    return late_by_seconds, late_by_seconds > allowed_lateness_seconds


def build_offline_feature_rows(
    event: dict[str, Any],
    sequence_payload: dict[str, Any],
    aggregate_payload: dict[str, Any],
    item_payload: dict[str, Any],
    source_topic: str,
    allowed_lateness_seconds: int,
) -> dict[str, list[dict[str, Any]]]:
    late_by_seconds, is_late = late_arrival_metrics(event, allowed_lateness_seconds)
    feature_ts = parse_event_time(event["event_timestamp"])
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


build_warehouse_rows = build_offline_feature_rows


def _event_time_pair(event: dict[str, Any]) -> tuple[datetime, str]:
    feature_ts = parse_event_time(event["event_timestamp"])
    return feature_ts, isoformat_utc(feature_ts)


def build_postgres_feast_rows(
    event: dict[str, Any],
    sequence_payload: dict[str, Any],
    aggregate_payload: dict[str, Any],
    item_payload: dict[str, Any],
) -> dict[str, list[dict[str, Any]]]:
    feature_ts, feature_ts_text = _event_time_pair(event)
    created_ts = datetime.now(timezone.utc)
    return {
        "user_sequence_features": [
            {
                "user_id": int(sequence_payload["user_id"]),
                "feature_timestamp": feature_ts,
                "event_timestamp": feature_ts,
                "created_timestamp": created_ts,
                "hist_item_ids": [int(value) for value in sequence_payload["item_ids"]],
                "hist_event_type_ids": [int(value) for value in sequence_payload["event_type_ids"]],
                "hist_category_ids": [int(value) for value in sequence_payload["category_ids"]],
                "hist_brand_ids": [int(value) for value in sequence_payload["brand_ids"]],
                "hist_price_bucket_ids": [int(value) for value in sequence_payload["price_bucket_ids"]],
                "hist_event_timestamps": [str(value) for value in sequence_payload["event_timestamps"]],
                "hist_request_ids": [str(value) for value in sequence_payload["request_ids"]],
                "hist_impression_ids": [str(value) for value in sequence_payload["impression_ids"]],
                "hist_length": int(sequence_payload["sequence_length"]),
                "max_history_length": int(sequence_payload["max_history_length"]),
                "feature_version": str(sequence_payload["feature_version"]),
            }
        ],
        "user_aggregate_features": [
            {
                "user_id": int(aggregate_payload["user_id"]),
                "feature_timestamp": feature_ts,
                "event_timestamp": feature_ts,
                "views_30m": int(aggregate_payload["views_30m"]),
                "carts_30m": int(aggregate_payload["carts_30m"]),
                "purchases_24h": int(aggregate_payload["purchases_24h"]),
                "distinct_categories_7d": int(aggregate_payload["distinct_categories_7d"]),
                "avg_viewed_price_7d": float(aggregate_payload["avg_viewed_price_7d"]),
                "cart_to_purchase_ratio_7d": float(aggregate_payload["cart_to_purchase_ratio_7d"]),
                "last_event_age_seconds": int(aggregate_payload["last_event_age_seconds"]),
                "aggregation_window_end_ts": aggregate_payload.get("updated_at", feature_ts_text),
                "watermark_ts": feature_ts,
                "created_timestamp": created_ts,
                "feature_version": str(aggregate_payload["feature_version"]),
            }
        ],
        "item_features": [
            {
                "product_id": int(item_payload["product_id"]),
                "feature_timestamp": feature_ts,
                "event_timestamp": feature_ts,
                "category_id": int(item_payload["category_id"]),
                "brand_id": int(item_payload["brand_id"]),
                "price_bucket": int(item_payload["price_bucket"]),
                "is_active": bool(item_payload["is_active"]),
                "views_1h": int(item_payload["views_1h"]),
                "views_24h": int(item_payload["views_24h"]),
                "carts_1h": int(item_payload["carts_1h"]),
                "carts_24h": int(item_payload["carts_24h"]),
                "purchases_24h": int(item_payload["purchases_24h"]),
                "purchases_7d": int(item_payload["purchases_7d"]),
                "conversion_rate_7d": float(item_payload["conversion_rate_7d"]),
                "popularity_score": float(item_payload["popularity_score"]),
                "aggregation_window_end_ts": item_payload.get("updated_at", feature_ts_text),
                "watermark_ts": feature_ts,
                "created_timestamp": created_ts,
                "feature_version": str(item_payload["feature_version"]),
            }
        ],
    }


def build_late_event_dlq_row(
    event: dict[str, Any],
    source_topic: str,
    allowed_lateness_seconds: int,
    reason: str = "too_late_for_feature_update",
) -> dict[str, Any]:
    late_by_seconds, _ = late_arrival_metrics(event, allowed_lateness_seconds)
    created_ts = datetime.now(timezone.utc)
    event_ts = parse_event_time(event["event_timestamp"])
    return {
        "event_id": str(event["event_id"]),
        "user_id": int(event["user_id"]),
        "product_id": int(event["product_id"]),
        "event_type": str(event["event_type"]),
        "event_timestamp": event_ts,
        "processed_timestamp": created_ts,
        "late_by_seconds": late_by_seconds,
        "allowed_lateness_seconds": int(allowed_lateness_seconds),
        "source_topic": source_topic,
        "payload_hash": str(event.get("payload_hash") or ""),
        "reason": reason,
        "payload": json.dumps(event, default=str, sort_keys=True),
        "created_timestamp": created_ts,
    }


def apply_state_ttl(descriptor: Any, ttl_seconds: int) -> Any:
    if ttl_seconds <= 0:
        return descriptor
    from pyflink.common import Time
    from pyflink.datastream.state import StateTtlConfig

    ttl_config = (
        StateTtlConfig.new_builder(Time.seconds(ttl_seconds))
        .update_ttl_on_create_and_write()
        .never_return_expired()
        .build()
    )
    descriptor.enable_time_to_live(ttl_config)
    return descriptor


def user_sequence_payload_from_history(
    event: dict[str, Any],
    history: list[dict[str, Any]],
    max_history_length: int,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    state = UserSequenceState(max_history_length=max_history_length)
    user_id = int(event["user_id"])
    state.events_by_user[user_id] = deque(history, maxlen=max_history_length)
    payload = state.update(event)
    return payload, list(state.events_by_user[user_id])


def user_aggregate_payload_from_history(
    event: dict[str, Any],
    history: list[dict[str, Any]],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    state = UserAggregateState()
    user_id = int(event["user_id"])
    state.events_by_user[user_id] = deque(history)
    payload = state.update(event)
    return payload, list(state.events_by_user[user_id])


def item_payload_from_history(
    event: dict[str, Any],
    history: list[dict[str, Any]],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    state = ItemFeatureState()
    product_id = int(event["product_id"])
    state.events_by_product[product_id] = deque(history)
    payload = state.update(event)
    return payload, list(state.events_by_product[product_id])


def kafka_offsets_initializer(name: str):
    from pyflink.datastream.connectors.kafka import KafkaOffsetsInitializer

    if name == "earliest":
        return KafkaOffsetsInitializer.earliest()
    if name == "latest":
        return KafkaOffsetsInitializer.latest()
    if name == "committed-offsets":
        return KafkaOffsetsInitializer.committed_offsets()
    raise ValueError(f"Unsupported Kafka starting offsets: {name}")


def build_kafka_source(args: argparse.Namespace):
    from pyflink.common.serialization import SimpleStringSchema
    from pyflink.datastream.connectors.kafka import KafkaOffsetsInitializer, KafkaSource

    builder = (
        KafkaSource.builder()
        .set_bootstrap_servers(args.bootstrap_servers)
        .set_topics(args.topic)
        .set_group_id(args.group_id)
        .set_starting_offsets(kafka_offsets_initializer(args.starting_offsets))
        .set_value_only_deserializer(SimpleStringSchema())
        .set_client_id_prefix("recsys-native-pyflink")
        .set_property("fetch.max.bytes", str(args.kafka_fetch_max_bytes))
        .set_property("max.partition.fetch.bytes", str(args.kafka_max_partition_fetch_bytes))
        .set_property("max.poll.records", str(args.kafka_max_poll_records))
    )
    if not args.continuous and args.max_events > 0:
        builder = builder.set_bounded(KafkaOffsetsInitializer.latest())
    return builder.build()


def build_realtime_stream(env: Any, args: argparse.Namespace):
    from pyflink.common import Duration, Types, WatermarkStrategy
    from pyflink.datastream.functions import FilterFunction, KeyedProcessFunction, MapFunction
    from pyflink.datastream.state import ValueStateDescriptor
    try:
        from pyflink.common.watermark_strategy import TimestampAssigner
    except ImportError:
        from pyflink.datastream.functions import TimestampAssigner

    class ParseNormalizeEvent(MapFunction):
        def map(self, raw: str) -> dict[str, Any] | None:
            after = parse_message(raw)
            return normalize_event(after) if after is not None else None

    class EventTimestampAssigner(TimestampAssigner):
        def extract_timestamp(self, raw: str, record_timestamp: int) -> int:
            try:
                after = parse_message(raw)
                event = normalize_event(after) if after is not None else None
                if event is None:
                    raise ValueError("invalid CDC event")
                event_ts = parse_event_time(event["event_timestamp"])
                return int(event_ts.timestamp() * 1000)
            except Exception:
                if record_timestamp is not None and record_timestamp >= 0:
                    return int(record_timestamp)
                return int(datetime.now(timezone.utc).timestamp() * 1000)

    class KeepValidEvents(FilterFunction):
        def filter(self, value: dict[str, Any] | None) -> bool:
            return value is not None

    class LimitEvents(KeyedProcessFunction):
        def open(self, runtime_context):
            descriptor = apply_state_ttl(
                ValueStateDescriptor("native_limit_count", Types.LONG()),
                args.state_ttl_seconds,
            )
            self.count_state = runtime_context.get_state(descriptor)

        def process_element(self, event: dict[str, Any], ctx):
            count = self.count_state.value() or 0
            if args.max_events <= 0 or count < args.max_events:
                self.count_state.update(count + 1)
                yield event

    class MarkDuplicateEvents(KeyedProcessFunction):
        def open(self, runtime_context):
            descriptor = apply_state_ttl(
                ValueStateDescriptor("seen_event_id", Types.BOOLEAN()),
                args.dedup_state_ttl_seconds,
            )
            self.seen = runtime_context.get_state(descriptor)

        def process_element(self, event: dict[str, Any], ctx):
            duplicate = bool(self.seen.value())
            if not duplicate:
                self.seen.update(True)
            marked = dict(event)
            marked["_is_duplicate"] = duplicate
            yield marked

    class BuildUserFeatures(KeyedProcessFunction):
        def open(self, runtime_context):
            sequence_descriptor = apply_state_ttl(
                ValueStateDescriptor("user_sequence_history", Types.PICKLED_BYTE_ARRAY()),
                args.state_ttl_seconds,
            )
            aggregate_descriptor = apply_state_ttl(
                ValueStateDescriptor("user_aggregate_history", Types.PICKLED_BYTE_ARRAY()),
                args.state_ttl_seconds,
            )
            self.sequence_history = runtime_context.get_state(sequence_descriptor)
            self.aggregate_history = runtime_context.get_state(aggregate_descriptor)

        def process_element(self, event: dict[str, Any], ctx):
            if event.get("_is_duplicate"):
                yield {"event": event, "sequence_payload": None, "aggregate_payload": None}
                return
            sequence_payload, sequence_history = user_sequence_payload_from_history(
                event,
                self.sequence_history.value() or [],
                args.max_history_length,
            )
            aggregate_payload, aggregate_history = user_aggregate_payload_from_history(
                event,
                self.aggregate_history.value() or [],
            )
            self.sequence_history.update(sequence_history)
            self.aggregate_history.update(aggregate_history)
            yield {
                "event": event,
                "sequence_payload": sequence_payload,
                "aggregate_payload": aggregate_payload,
            }

    class BuildItemFeatures(KeyedProcessFunction):
        def open(self, runtime_context):
            descriptor = apply_state_ttl(
                ValueStateDescriptor("item_feature_history", Types.PICKLED_BYTE_ARRAY()),
                args.state_ttl_seconds,
            )
            self.item_history = runtime_context.get_state(descriptor)

        def process_element(self, envelope: dict[str, Any], ctx):
            event = envelope["event"]
            if event.get("_is_duplicate"):
                yield {**envelope, "item_payload": None}
                return
            item_payload, item_history = item_payload_from_history(
                event,
                self.item_history.value() or [],
            )
            self.item_history.update(item_history)
            yield {**envelope, "item_payload": item_payload}

    class RedisFeatureWriter(MapFunction):
        def open(self, runtime_context):
            import redis

            self.redis_client = redis.Redis(host=args.redis_host, port=args.redis_port, decode_responses=True)
            self.writer = RedisOnlineWriter(self.redis_client)
            self.writes = 0
            self.last_write_unixtime = 0

        def map(self, envelope: dict[str, Any]) -> str:
            event = envelope["event"]
            if event.get("_is_duplicate"):
                return json.dumps({"status": "duplicate_skipped", "event_id": event["event_id"]}, sort_keys=True)
            sequence_payload = envelope["sequence_payload"]
            aggregate_payload = envelope["aggregate_payload"]
            item_payload = envelope["item_payload"]
            writes = write_payloads_to_redis(
                event,
                self.writer,
                self.redis_client,
                sequence_payload,
                aggregate_payload,
                item_payload,
            )
            self.writes += writes
            if writes:
                self.last_write_unixtime = int(datetime.now(timezone.utc).timestamp())
            if args.progress_log_events > 0 and self.writes % args.progress_log_events == 0:
                emit_progress({"status": "running", "topic": args.topic, "redis_writes": self.writes})
            return json.dumps(
                {
                    "status": "redis_written",
                    "event_id": event["event_id"],
                    "redis_writes": self.writes,
                },
                sort_keys=True,
            )

    class RedisFeaturePassthrough(RedisFeatureWriter):
        def map(self, envelope: dict[str, Any]) -> dict[str, Any]:
            super().map(envelope)
            return envelope

    class PostgresFeastOfflineWriter(MapFunction):
        def open(self, runtime_context):
            self.config = PostgresOfflineStoreConfig(
                host=args.feast_postgres_host,
                port=args.feast_postgres_port,
                database=args.feast_postgres_database,
                schema=args.feast_postgres_schema,
                user=args.feast_postgres_user,
                password=args.feast_postgres_password,
                sslmode=args.feast_postgres_sslmode,
            )
            self.conn = self.config.connect()
            ensure_offline_store_tables(self.conn, self.config.schema, FEATURE_TABLES)
            self.writes = 0

        def map(self, envelope: dict[str, Any]) -> str:
            event = envelope["event"]
            if event.get("_is_duplicate"):
                return json.dumps({"status": "duplicate_skipped", "event_id": event["event_id"]}, sort_keys=True)
            rows_by_table = build_postgres_feast_rows(
                event,
                envelope["sequence_payload"],
                envelope["aggregate_payload"],
                envelope["item_payload"],
            )
            inserted = 0
            for table_name, rows in rows_by_table.items():
                inserted += insert_offline_rows(self.conn, self.config.schema, table_name, rows)
            self.writes += inserted
            if args.progress_log_events > 0 and self.writes % args.progress_log_events == 0:
                emit_progress(
                    {
                        "status": "running",
                        "topic": args.topic,
                        "offline_store_sink": "postgres",
                        "postgres_rows": self.writes,
                    }
                )
            return json.dumps(
                {
                    "status": "postgres_feast_offline_written",
                    "event_id": event["event_id"],
                    "rows": inserted,
                    "total_rows": self.writes,
                },
                sort_keys=True,
            )

        def close(self):
            self.conn.close()

    class KeepRows(FilterFunction):
        def filter(self, value: Any | None) -> bool:
            return value is not None

    class KeepFeatureEvents(FilterFunction):
        def filter(self, event: dict[str, Any]) -> bool:
            if not args.drop_late_events:
                return True
            _, is_late = late_arrival_metrics(event, args.allowed_lateness_seconds)
            return not is_late

    class KeepLateEvents(FilterFunction):
        def filter(self, event: dict[str, Any]) -> bool:
            _, is_late = late_arrival_metrics(event, args.allowed_lateness_seconds)
            return is_late

    class PostgresLateEventDlqWriter(MapFunction):
        def open(self, runtime_context):
            self.config = PostgresOfflineStoreConfig(
                host=args.feast_postgres_host,
                port=args.feast_postgres_port,
                database=args.feast_postgres_database,
                schema=args.feast_postgres_schema,
                user=args.feast_postgres_user,
                password=args.feast_postgres_password,
                sslmode=args.feast_postgres_sslmode,
            )
            self.conn = self.config.connect()
            ensure_offline_store_tables(self.conn, self.config.schema, ("stream_late_events_dlq",))
            self.writes = 0

        def map(self, event: dict[str, Any]) -> str:
            row = build_late_event_dlq_row(event, args.topic, args.allowed_lateness_seconds)
            self.writes += insert_offline_rows(self.conn, self.config.schema, "stream_late_events_dlq", [row])
            return json.dumps(
                {
                    "status": "late_event_dlq_written",
                    "event_id": event["event_id"],
                    "late_by_seconds": float(row["late_by_seconds"]),
                    "total_rows": self.writes,
                },
                sort_keys=True,
            )

        def close(self):
            self.conn.close()

    class StreamBehaviorEventRow(MapFunction):
        def map(self, envelope: dict[str, Any]):
            from pyflink.common import Row

            event = envelope["event"]
            if event.get("_is_duplicate"):
                return None
            row = build_offline_feature_rows(
                event,
                envelope["sequence_payload"],
                envelope["aggregate_payload"],
                envelope["item_payload"],
                args.topic,
                args.allowed_lateness_seconds,
            )["stream_behavior_events"][0]
            return Row(
                row["event_id"],
                flink_timestamp(row["event_timestamp"]),
                flink_timestamp(row["processed_timestamp"]),
                row["user_id"],
                row["product_id"],
                row["event_type"],
                row["event_type_id"],
                row["category_id"],
                row["brand_id"],
                row["price"],
                row["price_bucket"],
                row["payload_hash"],
                row["source_topic"],
                row["late_by_seconds"],
                row["is_late"],
            )

    class UserSequenceFeatureRow(MapFunction):
        def map(self, envelope: dict[str, Any]):
            from pyflink.common import Row

            event = envelope["event"]
            if event.get("_is_duplicate"):
                return None
            row = build_offline_feature_rows(
                event,
                envelope["sequence_payload"],
                envelope["aggregate_payload"],
                envelope["item_payload"],
                args.topic,
                args.allowed_lateness_seconds,
            )["stream_user_sequence_features"][0]
            return Row(
                row["user_id"],
                flink_timestamp(row["feature_timestamp"]),
                row["sequence_length"],
                row["max_history_length"],
                dumps_feature_payload(row["feature_payload"]),
                row["feature_version"],
            )

    class UserAggregateFeatureRow(MapFunction):
        def map(self, envelope: dict[str, Any]):
            from pyflink.common import Row

            event = envelope["event"]
            if event.get("_is_duplicate"):
                return None
            row = build_offline_feature_rows(
                event,
                envelope["sequence_payload"],
                envelope["aggregate_payload"],
                envelope["item_payload"],
                args.topic,
                args.allowed_lateness_seconds,
            )["stream_user_aggregate_features"][0]
            return Row(
                row["user_id"],
                flink_timestamp(row["feature_timestamp"]),
                row["views_30m"],
                row["carts_30m"],
                row["purchases_24h"],
                dumps_feature_payload(row["feature_payload"]),
                row["feature_version"],
            )

    class ItemFeatureRow(MapFunction):
        def map(self, envelope: dict[str, Any]):
            from pyflink.common import Row

            event = envelope["event"]
            if event.get("_is_duplicate"):
                return None
            row = build_offline_feature_rows(
                event,
                envelope["sequence_payload"],
                envelope["aggregate_payload"],
                envelope["item_payload"],
                args.topic,
                args.allowed_lateness_seconds,
            )["stream_item_features"][0]
            return Row(
                row["product_id"],
                flink_timestamp(row["feature_timestamp"]),
                row["category_id"],
                row["brand_id"],
                row["price_bucket"],
                row["views_1h"],
                row["views_24h"],
                row["purchases_24h"],
                row["popularity_score"],
                dumps_feature_payload(row["feature_payload"]),
                row["feature_version"],
            )

    class StreamingQualityRows(KeyedProcessFunction):
        def open(self, runtime_context):
            descriptor = apply_state_ttl(
                ValueStateDescriptor("stream_quality_window", Types.PICKLED_BYTE_ARRAY()),
                args.state_ttl_seconds,
            )
            self.window_state = runtime_context.get_state(descriptor)
            self.last_event_unixtime = 0
            self.current_max_late_seconds = 0
            self.current_window_bursty = 0

        def _row(self, window: dict[str, Any]) -> dict[str, Any]:
            return {
                "window_start": parse_event_time(window["window_start"]),
                "window_end": parse_event_time(window["window_end"]),
                "topic": args.topic,
                "event_count": int(window["event_count"]),
                "late_event_count": int(window["late_event_count"]),
                "late_events_dropped": int(window["late_events_dropped"]),
                "side_output_late_events": int(window["side_output_late_events"]),
                "duplicate_event_count": int(window["duplicate_event_count"]),
                "max_late_by_seconds": float(window["max_late_by_seconds"]),
                "is_bursty": bool(window["event_count"] >= args.burst_threshold_event_count),
                "created_timestamp": datetime.now(timezone.utc),
            }

        def process_element(self, event: dict[str, Any], ctx):
            late_by_seconds, is_late = late_arrival_metrics(event, args.allowed_lateness_seconds)
            is_duplicate = bool(event.get("_is_duplicate"))
            self.last_event_unixtime = int(datetime.now(timezone.utc).timestamp())
            event_ts = parse_event_time(event["event_timestamp"])
            event_unix_seconds = int(event_ts.timestamp())
            window_start_seconds = event_unix_seconds - (event_unix_seconds % args.quality_window_seconds)
            window_start = datetime.fromtimestamp(window_start_seconds, tz=timezone.utc)
            window_end = window_start + timedelta(seconds=args.quality_window_seconds)
            window_start_text = isoformat_utc(window_start)
            current = self.window_state.value()
            rows = []
            if current is not None and current["window_start"] != window_start_text:
                rows.append(self._row(current))
                current = None
            if current is None:
                current = {
                    "window_start": window_start_text,
                    "window_end": isoformat_utc(window_end),
                    "event_count": 0,
                    "late_event_count": 0,
                    "late_events_dropped": 0,
                    "side_output_late_events": 0,
                    "duplicate_event_count": 0,
                    "max_late_by_seconds": 0.0,
                }
                self.current_max_late_seconds = 0
                self.current_window_bursty = 0
            current["event_count"] += 1
            current["late_event_count"] += 1 if is_late else 0
            current["late_events_dropped"] += 1 if is_late and args.drop_late_events else 0
            current["side_output_late_events"] += 1 if is_late else 0
            current["duplicate_event_count"] += 1 if is_duplicate else 0
            current["max_late_by_seconds"] = max(float(current["max_late_by_seconds"]), late_by_seconds)
            self.current_max_late_seconds = int(round(float(current["max_late_by_seconds"])))
            self.current_window_bursty = int(current["event_count"] >= args.burst_threshold_event_count)
            self.window_state.update(current)
            rows.append(self._row(current))
            for row in rows:
                yield row

    class QualityWindowRow(MapFunction):
        def map(self, row: dict[str, Any]):
            from pyflink.common import Row

            return Row(
                flink_timestamp(row["window_start"]),
                flink_timestamp(row["window_end"]),
                row["topic"],
                row["event_count"],
                row["late_event_count"],
                row["late_events_dropped"],
                row["side_output_late_events"],
                row["duplicate_event_count"],
                row["max_late_by_seconds"],
                row["is_bursty"],
                flink_timestamp(row["created_timestamp"]),
            )

    class LateEventDlqRow(MapFunction):
        def map(self, event: dict[str, Any]):
            from pyflink.common import Row

            row = build_late_event_dlq_row(event, args.topic, args.allowed_lateness_seconds)
            return Row(
                row["event_id"],
                row["user_id"],
                row["product_id"],
                row["event_type"],
                flink_timestamp(row["event_timestamp"]),
                flink_timestamp(row["processed_timestamp"]),
                row["late_by_seconds"],
                row["allowed_lateness_seconds"],
                row["source_topic"],
                row["payload_hash"],
                row["reason"],
                row["payload"],
                flink_timestamp(row["created_timestamp"]),
            )

    class QualityWindowMetricLog(MapFunction):
        def map(self, row: dict[str, Any]) -> str:
            return json.dumps(
                {
                    "status": "streaming_quality_window_metrics",
                    "window_start": isoformat_utc(row["window_start"]),
                    "window_end": isoformat_utc(row["window_end"]),
                    "topic": row["topic"],
                    "event_count": int(row["event_count"]),
                    "late_event_count": int(row["late_event_count"]),
                    "late_events_dropped": int(row["late_events_dropped"]),
                    "side_output_late_events": int(row["side_output_late_events"]),
                    "duplicate_event_count": int(row["duplicate_event_count"]),
                    "max_late_by_seconds": float(row["max_late_by_seconds"]),
                    "is_bursty": bool(row["is_bursty"]),
                    "drop_late_events": bool(args.drop_late_events),
                },
                sort_keys=True,
            )

    source = build_kafka_source(args)
    watermark = WatermarkStrategy.for_bounded_out_of_orderness(
        Duration.of_minutes(args.watermark_delay_minutes)
    ).with_timestamp_assigner(EventTimestampAssigner())
    if args.watermark_idleness_seconds > 0:
        watermark = watermark.with_idleness(Duration.of_seconds(args.watermark_idleness_seconds))
    if args.watermark_alignment_enabled:
        watermark = watermark.with_watermark_alignment(
            args.watermark_alignment_group,
            Duration.of_seconds(args.watermark_alignment_max_drift_seconds),
            Duration.of_seconds(args.watermark_alignment_update_interval_seconds),
        )
    raw_stream = env.from_source(source, watermark, "cdc-behavior-events-source")
    parsed = raw_stream.map(
        ParseNormalizeEvent(),
        output_type=Types.PICKLED_BYTE_ARRAY(),
    ).filter(KeepValidEvents())
    if not args.continuous and args.max_events > 0:
        parsed = parsed.key_by(lambda event: "native-bounded-limit").process(
            LimitEvents(),
            output_type=Types.PICKLED_BYTE_ARRAY(),
        )
    deduped = parsed.key_by(lambda event: str(event["event_id"])).process(
        MarkDuplicateEvents(),
        output_type=Types.PICKLED_BYTE_ARRAY(),
    )
    quality_rows = deduped.key_by(lambda event: "stream-quality").process(
        StreamingQualityRows(),
        output_type=Types.PICKLED_BYTE_ARRAY(),
    )
    quality_rows.map(
        QualityWindowMetricLog(),
        output_type=Types.STRING(),
    ).name("streaming-quality-window-metrics").print()
    late_events = deduped.filter(KeepLateEvents()).name("late-events-side-output")
    if args.offline_store_enabled and args.offline_store_sink == "postgres" and args.enable_late_event_dlq:
        late_events.map(
            PostgresLateEventDlqWriter(),
            output_type=Types.STRING(),
        ).name("postgres-late-events-dlq").print()
    feature_events = deduped.filter(KeepFeatureEvents()).name("late-event-drop-policy")
    user_features = feature_events.key_by(lambda event: int(event["user_id"])).process(
        BuildUserFeatures(),
        output_type=Types.PICKLED_BYTE_ARRAY(),
    )
    enriched = user_features.key_by(lambda envelope: int(envelope["event"]["product_id"])).process(
        BuildItemFeatures(),
        output_type=Types.PICKLED_BYTE_ARRAY(),
    )
    if not args.disable_online_store:
        enriched = enriched.map(
            RedisFeaturePassthrough(),
            output_type=Types.PICKLED_BYTE_ARRAY(),
        ).name("redis-online-feature-writer")
    else:
        emit_progress({"status": "online_store_disabled", "topic": args.topic, "group_id": args.group_id})
    if not args.offline_store_enabled:
        return None

    if args.offline_store_sink == "postgres":
        enriched.map(
            PostgresFeastOfflineWriter(),
            output_type=Types.STRING(),
        ).name("postgres-feast-offline-feature-writer").print()
        return None

    from features.flink.iceberg_feature_sink import configure_iceberg_catalog
    from lakehouse.iceberg import IcebergCatalogConfig
    from pyflink.table import StreamTableEnvironment

    catalog = IcebergCatalogConfig(
        catalog_name=args.iceberg_catalog,
        offline_feature_catalog_name=args.offline_feature_catalog,
        feature_namespace=args.iceberg_feature_namespace,
        warehouse_uri=args.lakehouse_warehouse,
        offline_feature_warehouse_uri=args.offline_feature_store_warehouse,
    )
    table_env = StreamTableEnvironment.create(env)
    configure_iceberg_catalog(table_env, catalog)
    statement_set = table_env.create_statement_set()

    def add_insert(name: str, stream: Any) -> None:
        table = table_env.from_data_stream(stream)
        statement_set.add_insert(catalog.feature_table(name), table)

    behavior_stream = enriched.map(
        StreamBehaviorEventRow(),
        output_type=Types.ROW_NAMED(
            [
                "event_id", "event_timestamp", "processed_timestamp", "user_id", "product_id",
                "event_type", "event_type_id", "category_id", "brand_id", "price", "price_bucket",
                "payload_hash", "source_topic", "late_by_seconds", "is_late",
            ],
            [
                Types.STRING(), Types.SQL_TIMESTAMP(), Types.SQL_TIMESTAMP(), Types.LONG(), Types.LONG(),
                Types.STRING(), Types.INT(), Types.INT(), Types.INT(), Types.DOUBLE(), Types.INT(),
                Types.STRING(), Types.STRING(), Types.DOUBLE(), Types.BOOLEAN(),
            ],
        ),
    ).filter(KeepRows())
    add_insert("stream_behavior_events", behavior_stream)
    add_insert(
        "stream_user_sequence_features",
        enriched.map(
            UserSequenceFeatureRow(),
            output_type=Types.ROW_NAMED(
                ["user_id", "feature_timestamp", "sequence_length", "max_history_length", "feature_payload", "feature_version"],
                [Types.LONG(), Types.SQL_TIMESTAMP(), Types.INT(), Types.INT(), Types.STRING(), Types.STRING()],
            ),
        ).filter(KeepRows()),
    )
    add_insert(
        "stream_user_aggregate_features",
        enriched.map(
            UserAggregateFeatureRow(),
            output_type=Types.ROW_NAMED(
                ["user_id", "feature_timestamp", "views_30m", "carts_30m", "purchases_24h", "feature_payload", "feature_version"],
                [Types.LONG(), Types.SQL_TIMESTAMP(), Types.INT(), Types.INT(), Types.INT(), Types.STRING(), Types.STRING()],
            ),
        ).filter(KeepRows()),
    )
    add_insert(
        "stream_item_features",
        enriched.map(
            ItemFeatureRow(),
            output_type=Types.ROW_NAMED(
                [
                    "product_id", "feature_timestamp", "category_id", "brand_id", "price_bucket",
                    "views_1h", "views_24h", "purchases_24h", "popularity_score", "feature_payload", "feature_version",
                ],
                [
                    Types.LONG(), Types.SQL_TIMESTAMP(), Types.INT(), Types.INT(), Types.INT(),
                    Types.INT(), Types.INT(), Types.INT(), Types.DOUBLE(), Types.STRING(), Types.STRING(),
                ],
            ),
        ).filter(KeepRows()),
    )
    add_insert(
        "streaming_quality_windows",
        quality_rows.map(
            QualityWindowRow(),
            output_type=Types.ROW_NAMED(
                [
                    "window_start", "window_end", "topic", "event_count", "late_event_count",
                    "late_events_dropped", "side_output_late_events", "duplicate_event_count",
                    "max_late_by_seconds", "is_bursty", "created_timestamp",
                ],
                [
                    Types.SQL_TIMESTAMP(), Types.SQL_TIMESTAMP(), Types.STRING(), Types.LONG(), Types.LONG(),
                    Types.LONG(), Types.LONG(), Types.LONG(), Types.DOUBLE(), Types.BOOLEAN(), Types.SQL_TIMESTAMP(),
                ],
            ),
        ),
    )
    add_insert(
        "stream_late_events_dlq",
        late_events.map(
            LateEventDlqRow(),
            output_type=Types.ROW_NAMED(
                [
                    "event_id",
                    "user_id",
                    "product_id",
                    "event_type",
                    "event_timestamp",
                    "processed_timestamp",
                    "late_by_seconds",
                    "allowed_lateness_seconds",
                    "source_topic",
                    "payload_hash",
                    "reason",
                    "payload",
                    "created_timestamp",
                ],
                [
                    Types.STRING(),
                    Types.LONG(),
                    Types.LONG(),
                    Types.STRING(),
                    Types.SQL_TIMESTAMP(),
                    Types.SQL_TIMESTAMP(),
                    Types.DOUBLE(),
                    Types.LONG(),
                    Types.STRING(),
                    Types.STRING(),
                    Types.STRING(),
                    Types.STRING(),
                    Types.SQL_TIMESTAMP(),
                ],
            ),
        ),
    )
    return statement_set


def run_pyflink_stream(args: argparse.Namespace) -> None:
    from pyflink.datastream import StreamExecutionEnvironment

    env = StreamExecutionEnvironment.get_execution_environment()
    env.set_parallelism(args.parallelism)
    env.enable_checkpointing(args.checkpoint_interval_seconds * 1000)
    statement_set = build_realtime_stream(env, args)
    if statement_set is None:
        env.execute(f"recsys-native-pyflink-realtime-features-online-{args.group_id}")
    else:
        statement_set.execute().wait()


def main() -> int:
    parser = argparse.ArgumentParser(description="Native PyFlink Kafka realtime feature job.")
    parser.add_argument("--runner", choices=["pyflink"], default=os.getenv("STREAM_RUNNER", "pyflink"))
    parser.add_argument("--topic", default="cdc.behavior_events")
    parser.add_argument("--bootstrap-servers", default=os.getenv("KAFKA_BOOTSTRAP_SERVERS", "kafka:29092"))
    parser.add_argument("--redis-host", default=os.getenv("REDIS_HOST", "redis"))
    parser.add_argument("--redis-port", type=int, default=env_int("REDIS_PORT", 6379))
    parser.add_argument("--group-id", default="recsys-flink-realtime-local")
    parser.add_argument("--starting-offsets", choices=["earliest", "latest", "committed-offsets"], default="earliest")
    parser.add_argument("--kafka-fetch-max-bytes", type=int, default=env_int("KAFKA_FETCH_MAX_BYTES", 1048576))
    parser.add_argument(
        "--kafka-max-partition-fetch-bytes",
        type=int,
        default=env_int("KAFKA_MAX_PARTITION_FETCH_BYTES", 262144),
    )
    parser.add_argument("--kafka-max-poll-records", type=int, default=env_int("KAFKA_MAX_POLL_RECORDS", 100))
    parser.add_argument("--max-events", type=int, default=200)
    parser.add_argument("--min-events", type=int, default=1)
    parser.add_argument("--continuous", action="store_true")
    parser.add_argument("--parallelism", type=int, default=env_int("FLINK_PARALLELISM", 1))
    parser.add_argument("--max-history-length", type=int, default=50)
    parser.add_argument("--offline-store-enabled", action="store_true", default=os.getenv("OFFLINE_STORE_ENABLED", "true").lower() in {"1", "true", "yes"})
    parser.add_argument("--disable-offline-store", action="store_true", default=os.getenv("DISABLE_OFFLINE_STORE", "false").lower() in {"1", "true", "yes"})
    parser.add_argument("--disable-online-store", action="store_true", default=os.getenv("DISABLE_ONLINE_STORE", "false").lower() in {"1", "true", "yes"})
    parser.add_argument("--lakehouse-warehouse", default=os.getenv("LAKEHOUSE_WAREHOUSE", "s3a://recsys-lakehouse/warehouse"))
    parser.add_argument("--iceberg-catalog", default=os.getenv("ICEBERG_CATALOG", "recsys"))
    parser.add_argument("--offline-feature-catalog", default=os.getenv("OFFLINE_FEATURE_CATALOG", "recsys_features"))
    parser.add_argument("--offline-feature-store-warehouse", default=os.getenv("OFFLINE_FEATURE_STORE_WAREHOUSE", "s3a://recsys-offline-feature-store/warehouse"))
    parser.add_argument("--iceberg-feature-namespace", default=os.getenv("ICEBERG_FEATURE_NAMESPACE", "feature_store"))
    parser.add_argument("--offline-store-sink", choices=["postgres", "iceberg"], default=os.getenv("OFFLINE_STORE_SINK", "iceberg"))
    parser.add_argument("--feast-postgres-host", default=os.getenv("FEAST_POSTGRES_HOST", "feature-postgres"))
    parser.add_argument("--feast-postgres-port", type=int, default=env_int("FEAST_POSTGRES_PORT", 5432))
    parser.add_argument("--feast-postgres-database", default=os.getenv("FEAST_POSTGRES_DB", "feature_store"))
    parser.add_argument("--feast-postgres-schema", default=os.getenv("FEAST_POSTGRES_SCHEMA", "feature_store"))
    parser.add_argument("--feast-postgres-user", default=os.getenv("FEAST_POSTGRES_USER", "feast"))
    parser.add_argument("--feast-postgres-password", default=os.getenv("FEAST_POSTGRES_PASSWORD", "feast"))
    parser.add_argument("--feast-postgres-sslmode", default=os.getenv("FEAST_POSTGRES_SSLMODE", "disable"))
    parser.add_argument("--watermark-delay-minutes", type=int, default=env_int("STREAM_WATERMARK_DELAY_MINUTES", 60))
    parser.add_argument("--allowed-lateness-seconds", type=int, default=env_int("STREAM_ALLOWED_LATENESS_SECONDS", 300))
    parser.add_argument("--watermark-idleness-seconds", type=int, default=env_int("STREAM_WATERMARK_IDLENESS_SECONDS", 120))
    parser.add_argument(
        "--watermark-alignment-enabled",
        action=argparse.BooleanOptionalAction,
        default=env_bool("STREAM_WATERMARK_ALIGNMENT_ENABLED", False),
    )
    parser.add_argument("--watermark-alignment-group", default=os.getenv("STREAM_WATERMARK_ALIGNMENT_GROUP", "recsys-cdc"))
    parser.add_argument(
        "--watermark-alignment-max-drift-seconds",
        type=int,
        default=env_int("STREAM_WATERMARK_ALIGNMENT_MAX_DRIFT_SECONDS", 60),
    )
    parser.add_argument(
        "--watermark-alignment-update-interval-seconds",
        type=int,
        default=env_int("STREAM_WATERMARK_ALIGNMENT_UPDATE_INTERVAL_SECONDS", 5),
    )
    parser.add_argument("--quality-window-seconds", type=int, default=env_int("STREAM_QUALITY_WINDOW_SECONDS", 60))
    parser.add_argument("--burst-threshold-event-count", type=int, default=env_int("STREAM_BURST_THRESHOLD_EVENT_COUNT", 500))
    parser.add_argument(
        "--drop-late-events",
        action=argparse.BooleanOptionalAction,
        default=env_bool("STREAM_DROP_LATE_EVENTS", True),
    )
    parser.add_argument(
        "--enable-late-event-dlq",
        action=argparse.BooleanOptionalAction,
        default=env_bool("STREAM_ENABLE_LATE_EVENT_DLQ", True),
    )
    parser.add_argument("--state-ttl-seconds", type=int, default=env_int("STREAM_STATE_TTL_SECONDS", 7 * 24 * 60 * 60))
    parser.add_argument("--dedup-state-ttl-seconds", type=int, default=env_int("STREAM_DEDUP_STATE_TTL_SECONDS", 24 * 60 * 60))
    parser.add_argument("--progress-log-events", type=int, default=env_int("STREAM_PROGRESS_LOG_EVENTS", 100))
    parser.add_argument("--checkpoint-interval-seconds", type=int, default=env_int("STREAM_CHECKPOINT_INTERVAL_SECONDS", 30))
    args = parser.parse_args()
    if args.disable_offline_store:
        args.offline_store_enabled = False

    offline_outputs: set[str] = set()
    if args.offline_store_enabled:
        configured_outputs = POSTGRES_FEATURE_URNS if args.offline_store_sink == "postgres" else ICEBERG_FEATURE_URNS
        offline_outputs.update(configured_outputs[table] for table in REDIS_FEATURE_URNS)
    runtime_run_id = lineage_run_id()
    recorders: list[RuntimeLineageRecorder] = []
    if args.offline_store_enabled:
        recorders.append(
            RuntimeLineageRecorder(
                "STREAMING_FEATURES",
                "run_flink_stream_to_offline_store",
                inputs={KAFKA_TOPIC_URNS["behavior_events"]},
                outputs=offline_outputs,
                run_id=runtime_run_id,
            )
        )
    if not args.disable_online_store:
        recorders.append(
            RuntimeLineageRecorder(
                "STREAMING_FEATURES",
                "run_flink_stream_to_online_store",
                inputs={KAFKA_TOPIC_URNS["behavior_events"]},
                outputs=set(REDIS_FEATURE_URNS.values()),
                run_id=runtime_run_id,
            )
        )
    for recorder in recorders:
        recorder.__enter__()
    try:
        run_pyflink_stream(args)
    except Exception as exc:
        for recorder in recorders:
            recorder.fail(str(exc))
        raise
    else:
        for recorder in recorders:
            recorder.complete()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
