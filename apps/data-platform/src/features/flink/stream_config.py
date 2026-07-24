from __future__ import annotations

import argparse
import os
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class StreamConfig:
    """Immutable, pickle-friendly view of the parsed stream configuration."""

    values: dict[str, Any]

    @classmethod
    def from_namespace(cls, namespace: argparse.Namespace) -> StreamConfig:
        return cls(values=dict(vars(namespace)))

    def __getattr__(self, name: str) -> Any:
        try:
            return self.values[name]
        except KeyError as exc:
            raise AttributeError(name) from exc


def env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.lower() in {"1", "true", "yes", "on"}


def stream_pipeline_role(args: StreamConfig | argparse.Namespace) -> str:
    if args.disable_offline_store and not args.disable_online_store:
        return "online"
    if args.disable_online_store and args.offline_store_enabled:
        return "offline"
    if args.disable_online_store and not args.offline_store_enabled:
        return "disabled"
    return "hybrid"


def build_stream_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Native PyFlink Kafka realtime feature job."
    )
    parser.add_argument(
        "--runner", choices=["pyflink"], default=os.getenv("STREAM_RUNNER", "pyflink")
    )
    parser.add_argument("--topic", default="cdc.behavior_events")
    parser.add_argument(
        "--bootstrap-servers",
        default=os.getenv("KAFKA_BOOTSTRAP_SERVERS", "kafka:29092"),
    )
    parser.add_argument("--redis-host", default=os.getenv("REDIS_HOST", "redis"))
    parser.add_argument("--redis-port", type=int, default=env_int("REDIS_PORT", 6379))
    parser.add_argument("--group-id", default="recsys-flink-realtime-local")
    parser.add_argument(
        "--starting-offsets",
        choices=["earliest", "latest", "committed-offsets"],
        default="earliest",
    )
    parser.add_argument(
        "--kafka-fetch-max-bytes",
        type=int,
        default=env_int("KAFKA_FETCH_MAX_BYTES", 1048576),
    )
    parser.add_argument(
        "--kafka-max-partition-fetch-bytes",
        type=int,
        default=env_int("KAFKA_MAX_PARTITION_FETCH_BYTES", 262144),
    )
    parser.add_argument(
        "--kafka-max-poll-records",
        type=int,
        default=env_int("KAFKA_MAX_POLL_RECORDS", 100),
    )
    parser.add_argument(
        "--redis-sink-max-events-per-second",
        type=float,
        default=float(os.getenv("REDIS_SINK_MAX_EVENTS_PER_SECOND", "200")),
    )
    parser.add_argument(
        "--postgres-sink-max-events-per-second",
        type=float,
        default=float(os.getenv("POSTGRES_SINK_MAX_EVENTS_PER_SECOND", "100")),
    )
    parser.add_argument(
        "--sink-rate-limit-burst-events",
        type=int,
        default=env_int("SINK_RATE_LIMIT_BURST_EVENTS", 25),
    )
    parser.add_argument(
        "--async-io-capacity", type=int, default=env_int("FLINK_ASYNC_IO_CAPACITY", 64)
    )
    parser.add_argument(
        "--async-io-timeout-seconds",
        type=int,
        default=env_int("FLINK_ASYNC_IO_TIMEOUT_SECONDS", 30),
    )
    parser.add_argument(
        "--postgres-async-pool-size",
        type=int,
        default=env_int("POSTGRES_ASYNC_POOL_SIZE", 16),
    )
    parser.add_argument("--max-events", type=int, default=200)
    parser.add_argument("--min-events", type=int, default=1)
    parser.add_argument("--continuous", action="store_true")
    parser.add_argument(
        "--parallelism", type=int, default=env_int("FLINK_PARALLELISM", 1)
    )
    parser.add_argument("--max-history-length", type=int, default=50)
    parser.add_argument(
        "--offline-store-enabled",
        action="store_true",
        default=env_bool("OFFLINE_STORE_ENABLED", True),
    )
    parser.add_argument(
        "--disable-offline-store",
        action="store_true",
        default=env_bool("DISABLE_OFFLINE_STORE", False),
    )
    parser.add_argument(
        "--disable-online-store",
        action="store_true",
        default=env_bool("DISABLE_ONLINE_STORE", False),
    )
    parser.add_argument(
        "--lakehouse-warehouse",
        default=os.getenv("LAKEHOUSE_WAREHOUSE", "s3a://recsys-lakehouse/warehouse"),
    )
    parser.add_argument(
        "--iceberg-catalog", default=os.getenv("ICEBERG_CATALOG", "recsys")
    )
    parser.add_argument(
        "--offline-feature-catalog",
        default=os.getenv("OFFLINE_FEATURE_CATALOG", "recsys_features"),
    )
    parser.add_argument(
        "--offline-feature-store-warehouse",
        default=os.getenv(
            "OFFLINE_FEATURE_STORE_WAREHOUSE",
            "s3a://recsys-offline-feature-store/warehouse",
        ),
    )
    parser.add_argument(
        "--iceberg-feature-namespace",
        default=os.getenv("ICEBERG_FEATURE_NAMESPACE", "feature_store"),
    )
    parser.add_argument(
        "--offline-store-sink",
        choices=["postgres", "iceberg"],
        default=os.getenv("OFFLINE_STORE_SINK", "postgres"),
    )
    parser.add_argument(
        "--feast-postgres-host",
        default=os.getenv("FEAST_POSTGRES_HOST", "feature-postgres"),
    )
    parser.add_argument(
        "--feast-postgres-port", type=int, default=env_int("FEAST_POSTGRES_PORT", 5432)
    )
    parser.add_argument(
        "--feast-postgres-database",
        default=os.getenv("FEAST_POSTGRES_DB", "feature_store"),
    )
    parser.add_argument(
        "--feast-postgres-schema",
        default=os.getenv("FEAST_POSTGRES_SCHEMA", "feature_store"),
    )
    parser.add_argument(
        "--feast-postgres-user", default=os.getenv("FEAST_POSTGRES_USER", "feast")
    )
    parser.add_argument(
        "--feast-postgres-password",
        default=os.getenv("FEAST_POSTGRES_PASSWORD", "feast"),
    )
    parser.add_argument(
        "--feast-postgres-sslmode",
        default=os.getenv("FEAST_POSTGRES_SSLMODE", "disable"),
    )
    parser.add_argument(
        "--watermark-delay-minutes",
        type=int,
        default=env_int("STREAM_WATERMARK_DELAY_MINUTES", 60),
    )
    parser.add_argument(
        "--allowed-lateness-seconds",
        type=int,
        default=env_int("STREAM_ALLOWED_LATENESS_SECONDS", 300),
    )
    parser.add_argument(
        "--watermark-idleness-seconds",
        type=int,
        default=env_int("STREAM_WATERMARK_IDLENESS_SECONDS", 120),
    )
    parser.add_argument(
        "--watermark-alignment-enabled",
        action=argparse.BooleanOptionalAction,
        default=env_bool("STREAM_WATERMARK_ALIGNMENT_ENABLED", False),
    )
    parser.add_argument(
        "--watermark-alignment-group",
        default=os.getenv("STREAM_WATERMARK_ALIGNMENT_GROUP", "recsys-cdc"),
    )
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
    parser.add_argument(
        "--quality-window-seconds",
        type=int,
        default=env_int("STREAM_QUALITY_WINDOW_SECONDS", 60),
    )
    parser.add_argument(
        "--feature-window-seconds",
        type=int,
        default=env_int("STREAM_FEATURE_WINDOW_SECONDS", 60),
    )
    parser.add_argument(
        "--feature-early-fire-seconds",
        type=int,
        default=env_int("STREAM_FEATURE_EARLY_FIRE_SECONDS", 5),
    )
    parser.add_argument(
        "--burst-threshold-event-count",
        type=int,
        default=env_int("STREAM_BURST_THRESHOLD_EVENT_COUNT", 500),
    )
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
    parser.add_argument(
        "--state-ttl-seconds",
        type=int,
        default=env_int("STREAM_STATE_TTL_SECONDS", 7 * 24 * 60 * 60),
    )
    parser.add_argument(
        "--dedup-state-ttl-seconds",
        type=int,
        default=env_int("STREAM_DEDUP_STATE_TTL_SECONDS", 24 * 60 * 60),
    )
    parser.add_argument(
        "--progress-log-events",
        type=int,
        default=env_int("STREAM_PROGRESS_LOG_EVENTS", 100),
    )
    parser.add_argument(
        "--checkpoint-interval-seconds",
        type=int,
        default=env_int("STREAM_CHECKPOINT_INTERVAL_SECONDS", 30),
    )
    parser.add_argument(
        "--checkpoint-min-pause-seconds",
        type=int,
        default=env_int("STREAM_CHECKPOINT_MIN_PAUSE_SECONDS", 10),
    )
    parser.add_argument(
        "--checkpoint-timeout-seconds",
        type=int,
        default=env_int("STREAM_CHECKPOINT_TIMEOUT_SECONDS", 300),
    )
    parser.add_argument(
        "--tolerable-checkpoint-failures",
        type=int,
        default=env_int("STREAM_TOLERABLE_CHECKPOINT_FAILURES", 2),
    )
    parser.add_argument(
        "--unaligned-checkpoints-enabled",
        action=argparse.BooleanOptionalAction,
        default=env_bool("STREAM_UNALIGNED_CHECKPOINTS_ENABLED", True),
    )
    return parser


def parse_stream_args(argv: Sequence[str] | None = None) -> StreamConfig:
    args = build_stream_parser().parse_args(argv)
    if args.disable_offline_store:
        args.offline_store_enabled = False
    return StreamConfig.from_namespace(args)
