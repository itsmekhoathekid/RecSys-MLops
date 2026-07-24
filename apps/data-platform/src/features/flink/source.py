from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from features.flink.pyflink_compat import (
    FilterFunction,
    KeyedProcessFunction,
    MapFunction,
    TimestampAssigner,
)
from features.flink.runtime import apply_state_ttl
from features.flink.time_utils import isoformat_utc, parse_event_time
from ingest.debezium import extract_debezium_after


EVENT_TYPE_IDS = {"view": 1, "cart": 2, "purchase": 3}


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
    event["event_type_id"] = safe_int(
        event.get("event_type_id"), EVENT_TYPE_IDS.get(event_type, 0)
    )
    event["category_id"] = safe_int(event.get("category_id"))
    event["brand_id"] = safe_int(event.get("brand_id"))
    event["price_bucket"] = safe_int(event.get("price_bucket"))
    event["price"] = safe_float(event.get("price"), float(event["price_bucket"]))
    event["user_id"] = safe_int(event["user_id"])
    event["product_id"] = safe_int(event["product_id"])
    event["event_timestamp"] = isoformat_utc(event["event_timestamp"])
    return event


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
            return int(parse_event_time(event["event_timestamp"]).timestamp() * 1000)
        except Exception:
            if record_timestamp is not None and record_timestamp >= 0:
                return int(record_timestamp)
            return int(datetime.now(timezone.utc).timestamp() * 1000)


class KeepValidEvents(FilterFunction):
    def filter(self, value: dict[str, Any] | None) -> bool:
        return value is not None


class LimitEvents(KeyedProcessFunction):
    def __init__(self, args: Any) -> None:
        self.args = args

    def open(self, runtime_context):
        from pyflink.common import Types
        from pyflink.datastream.state import ValueStateDescriptor

        descriptor = apply_state_ttl(
            ValueStateDescriptor("native_limit_count", Types.LONG()),
            self.args.state_ttl_seconds,
        )
        self.count_state = runtime_context.get_state(descriptor)

    def process_element(self, event: dict[str, Any], ctx):
        count = self.count_state.value() or 0
        if self.args.max_events <= 0 or count < self.args.max_events:
            self.count_state.update(count + 1)
            yield event


def kafka_offsets_initializer(name: str):
    from pyflink.datastream.connectors.kafka import KafkaOffsetsInitializer

    if name == "earliest":
        return KafkaOffsetsInitializer.earliest()
    if name == "latest":
        return KafkaOffsetsInitializer.latest()
    if name == "committed-offsets":
        return KafkaOffsetsInitializer.committed_offsets()
    raise ValueError(f"Unsupported Kafka starting offsets: {name}")


def build_kafka_source(args: Any):
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
        .set_property(
            "max.partition.fetch.bytes", str(args.kafka_max_partition_fetch_bytes)
        )
        .set_property("max.poll.records", str(args.kafka_max_poll_records))
    )
    if not args.continuous and args.max_events > 0:
        builder = builder.set_bounded(KafkaOffsetsInitializer.latest())
    return builder.build()


def build_watermark_strategy(args: Any, timestamp_assigner: Any):
    from pyflink.common import Duration, WatermarkStrategy

    watermark = WatermarkStrategy.for_bounded_out_of_orderness(
        Duration.of_minutes(args.watermark_delay_minutes)
    ).with_timestamp_assigner(timestamp_assigner)
    if args.watermark_idleness_seconds > 0:
        watermark = watermark.with_idleness(
            Duration.of_seconds(args.watermark_idleness_seconds)
        )
    if args.watermark_alignment_enabled:
        watermark = watermark.with_watermark_alignment(
            args.watermark_alignment_group,
            Duration.of_seconds(args.watermark_alignment_max_drift_seconds),
            Duration.of_seconds(args.watermark_alignment_update_interval_seconds),
        )
    return watermark
