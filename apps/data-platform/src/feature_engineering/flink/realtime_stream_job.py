from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from collections import deque
from typing import Any

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
from feature_engineering.flink.time_utils import isoformat_utc, parse_event_time
from feature_store.online_writer import RedisOnlineWriter
from ingest.bronze_cdc_reader import extract_debezium_after


EVENT_TYPE_IDS = {"view": 1, "cart": 2, "purchase": 3}


def emit_progress(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, sort_keys=True) + "\n")
    sys.stdout.flush()


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
    window_start: datetime
    window_end: datetime
    topic: str
    event_count: int = 0
    late_event_count: int = 0
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
    ) -> list[StreamQualityWindow]:
        ts = parse_event_time(event_timestamp)
        epoch = int(ts.timestamp())
        window_start_epoch = epoch - (epoch % self.window_seconds)
        window_start = datetime.fromtimestamp(window_start_epoch, tz=timezone.utc)
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
    processed_ts = datetime.now(timezone.utc)
    event_ts = parse_event_time(event["event_timestamp"])
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


def build_kafka_source(args: argparse.Namespace):
    from pyflink.common.serialization import SimpleStringSchema
    from pyflink.datastream.connectors.kafka import KafkaOffsetsInitializer, KafkaSource

    builder = (
        KafkaSource.builder()
        .set_bootstrap_servers(args.bootstrap_servers)
        .set_topics(args.topic)
        .set_group_id(args.group_id)
        .set_starting_offsets(KafkaOffsetsInitializer.earliest())
        .set_value_only_deserializer(SimpleStringSchema())
        .set_client_id_prefix("recsys-native-pyflink")
    )
    if not args.continuous and args.max_events > 0:
        builder = builder.set_bounded(KafkaOffsetsInitializer.latest())
    return builder.build()


def build_realtime_stream(env: Any, args: argparse.Namespace):
    from pyflink.common import Duration, Types, WatermarkStrategy
    from pyflink.datastream.functions import FilterFunction, KeyedProcessFunction, MapFunction
    from pyflink.datastream.state import ValueStateDescriptor

    class ParseNormalizeEvent(MapFunction):
        def map(self, raw: str) -> dict[str, Any] | None:
            after = parse_message(raw)
            return normalize_event(after) if after is not None else None

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

        def map(self, envelope: dict[str, Any]) -> str:
            event = envelope["event"]
            if event.get("_is_duplicate"):
                return json.dumps({"status": "duplicate_skipped", "event_id": event["event_id"]}, sort_keys=True)
            sequence_payload = envelope["sequence_payload"]
            aggregate_payload = envelope["aggregate_payload"]
            item_payload = envelope["item_payload"]
            self.writes += write_payloads_to_redis(
                event,
                self.writer,
                self.redis_client,
                sequence_payload,
                aggregate_payload,
                item_payload,
            )
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

    class WarehouseFeatureWriter(MapFunction):
        def open(self, runtime_context):
            self.connection = None
            if args.warehouse_enabled:
                from warehouse.connection import connect
                from warehouse.writer import ensure_warehouse

                self.connection = connect()
                ensure_warehouse(self.connection)

        def map(self, envelope: dict[str, Any]) -> str:
            if self.connection is None:
                return json.dumps({"status": "warehouse_disabled"}, sort_keys=True)
            event = envelope["event"]
            if event.get("_is_duplicate"):
                return json.dumps({"status": "warehouse_duplicate_skipped", "event_id": event["event_id"]}, sort_keys=True)
            rows = build_warehouse_rows(
                event,
                envelope["sequence_payload"],
                envelope["aggregate_payload"],
                envelope["item_payload"],
                args.topic,
                args.watermark_delay_minutes,
            )
            writes = write_warehouse_rows(self.connection, rows)
            return json.dumps({"status": "warehouse_written", "event_id": event["event_id"], "writes": writes}, sort_keys=True)

    class StreamingQualityRows(KeyedProcessFunction):
        def open(self, runtime_context):
            descriptor = apply_state_ttl(
                ValueStateDescriptor("stream_quality_window", Types.PICKLED_BYTE_ARRAY()),
                args.state_ttl_seconds,
            )
            self.window_state = runtime_context.get_state(descriptor)

        def _row(self, window: dict[str, Any]) -> dict[str, Any]:
            return {
                "window_start": parse_event_time(window["window_start"]),
                "window_end": parse_event_time(window["window_end"]),
                "topic": args.topic,
                "event_count": int(window["event_count"]),
                "late_event_count": int(window["late_event_count"]),
                "duplicate_event_count": int(window["duplicate_event_count"]),
                "max_late_by_seconds": float(window["max_late_by_seconds"]),
                "is_bursty": bool(window["event_count"] >= args.burst_threshold_event_count),
                "created_timestamp": datetime.now(timezone.utc),
            }

        def process_element(self, event: dict[str, Any], ctx):
            late_by_seconds, is_late = late_arrival_metrics(event, args.watermark_delay_minutes)
            event_ts = parse_event_time(event["event_timestamp"])
            epoch = int(event_ts.timestamp())
            window_start_epoch = epoch - (epoch % args.quality_window_seconds)
            window_start = datetime.fromtimestamp(window_start_epoch, tz=timezone.utc)
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
                    "duplicate_event_count": 0,
                    "max_late_by_seconds": 0.0,
                }
            current["event_count"] += 1
            current["late_event_count"] += 1 if is_late else 0
            current["duplicate_event_count"] += 1 if event.get("_is_duplicate") else 0
            current["max_late_by_seconds"] = max(float(current["max_late_by_seconds"]), late_by_seconds)
            self.window_state.update(current)
            rows.append(self._row(current))
            for row in rows:
                yield row

    class WarehouseQualityWriter(MapFunction):
        def open(self, runtime_context):
            self.connection = None
            if args.warehouse_enabled:
                from warehouse.connection import connect
                from warehouse.writer import ensure_warehouse

                self.connection = connect()
                ensure_warehouse(self.connection)

        def map(self, row: dict[str, Any]) -> str:
            if self.connection is None:
                return json.dumps({"status": "quality_warehouse_disabled"}, sort_keys=True, default=str)
            from warehouse.schemas import MONITORING_STREAMING_QUALITY_WINDOWS
            from warehouse.writer import upsert_rows

            writes = upsert_rows(self.connection, MONITORING_STREAMING_QUALITY_WINDOWS, [row])
            return json.dumps({"status": "quality_written", "writes": writes, **row}, sort_keys=True, default=str)

    source = build_kafka_source(args)
    watermark = WatermarkStrategy.for_bounded_out_of_orderness(
        Duration.of_minutes(args.watermark_delay_minutes)
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
    user_features = deduped.key_by(lambda event: int(event["user_id"])).process(
        BuildUserFeatures(),
        output_type=Types.PICKLED_BYTE_ARRAY(),
    )
    enriched = user_features.key_by(lambda envelope: int(envelope["event"]["product_id"])).process(
        BuildItemFeatures(),
        output_type=Types.PICKLED_BYTE_ARRAY(),
    )
    enriched.map(RedisFeatureWriter(), output_type=Types.STRING()).name("redis-online-feature-writer").print()
    enriched.map(WarehouseFeatureWriter(), output_type=Types.STRING()).name("warehouse-stream-feature-writer").print()
    deduped.key_by(lambda event: "stream-quality").process(
        StreamingQualityRows(),
        output_type=Types.PICKLED_BYTE_ARRAY(),
    ).map(WarehouseQualityWriter(), output_type=Types.STRING()).name("warehouse-stream-quality-writer").print()
    return enriched


def run_pyflink_stream(args: argparse.Namespace) -> None:
    from pyflink.datastream import StreamExecutionEnvironment

    env = StreamExecutionEnvironment.get_execution_environment()
    env.set_parallelism(args.parallelism)
    build_realtime_stream(env, args)
    env.execute("recsys-native-pyflink-realtime-features")


def main() -> int:
    parser = argparse.ArgumentParser(description="Native PyFlink Kafka realtime feature job.")
    parser.add_argument("--runner", choices=["pyflink"], default=os.getenv("STREAM_RUNNER", "pyflink"))
    parser.add_argument("--topic", default="cdc.behavior_events")
    parser.add_argument("--bootstrap-servers", default=os.getenv("KAFKA_BOOTSTRAP_SERVERS", "kafka:29092"))
    parser.add_argument("--redis-host", default=os.getenv("REDIS_HOST", "redis"))
    parser.add_argument("--redis-port", type=int, default=env_int("REDIS_PORT", 6379))
    parser.add_argument("--group-id", default="recsys-flink-realtime-local")
    parser.add_argument("--max-events", type=int, default=200)
    parser.add_argument("--min-events", type=int, default=1)
    parser.add_argument("--continuous", action="store_true")
    parser.add_argument("--parallelism", type=int, default=env_int("FLINK_PARALLELISM", 1))
    parser.add_argument("--max-history-length", type=int, default=50)
    parser.add_argument("--warehouse-enabled", action="store_true", default=os.getenv("WAREHOUSE_ENABLED", "").lower() in {"1", "true", "yes"})
    parser.add_argument("--watermark-delay-minutes", type=int, default=env_int("STREAM_WATERMARK_DELAY_MINUTES", 60))
    parser.add_argument("--quality-window-seconds", type=int, default=env_int("STREAM_QUALITY_WINDOW_SECONDS", 60))
    parser.add_argument("--burst-threshold-event-count", type=int, default=env_int("STREAM_BURST_THRESHOLD_EVENT_COUNT", 500))
    parser.add_argument("--state-ttl-seconds", type=int, default=env_int("STREAM_STATE_TTL_SECONDS", 7 * 24 * 60 * 60))
    parser.add_argument("--dedup-state-ttl-seconds", type=int, default=env_int("STREAM_DEDUP_STATE_TTL_SECONDS", 24 * 60 * 60))
    parser.add_argument("--progress-log-events", type=int, default=env_int("STREAM_PROGRESS_LOG_EVENTS", 100))
    args = parser.parse_args()

    run_pyflink_stream(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
