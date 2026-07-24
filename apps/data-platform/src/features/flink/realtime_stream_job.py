from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    source_root = str(Path(__file__).resolve().parents[2])
    script_dir = str(Path(__file__).resolve().parent)
    sys.path = [source_root, *(path for path in sys.path if path != script_dir)]

from features.flink.feature_windows import build_feature_update_streams
from features.flink.operators.dedup import MarkDuplicateEvents
from features.flink.operators.late_policy import (
    KeepFeatureEvents,
    MarkEventTimeStatus,
)
from features.flink.operators.quality import build_quality_window_streams
from features.flink.runtime import configure_checkpointing
from features.flink.sinks import emit_progress
from features.flink.sinks.iceberg import build_iceberg_statement_set
from features.flink.sinks.postgres_async import (
    AsyncPostgresFeastOfflineWriter,
    AsyncPostgresLateEventDlqWriter,
    postgres_async_capacity,
)
from features.flink.sinks.redis_async import AsyncRedisFeatureWriter
from features.flink.source import (
    EventTimestampAssigner,
    KeepValidEvents,
    LimitEvents,
    ParseNormalizeEvent,
    build_kafka_source,
    build_watermark_strategy,
)
from features.flink.stream_config import StreamConfig, parse_stream_args
from metadata.governance_catalog import (
    ICEBERG_FEATURE_URNS,
    KAFKA_TOPIC_URNS,
    POSTGRES_FEATURE_URNS,
    REDIS_FEATURE_URNS,
)
from metadata.runtime_lineage import RuntimeLineageRecorder, lineage_run_id


def _attach_postgres_dlq(late_events: Any, args: StreamConfig) -> None:
    if not (
        args.offline_store_enabled
        and args.offline_store_sink == "postgres"
        and args.enable_late_event_dlq
    ):
        return
    from pyflink.common import Time, Types
    from pyflink.datastream import AsyncDataStream

    AsyncDataStream.unordered_wait(
        data_stream=late_events,
        async_function=AsyncPostgresLateEventDlqWriter(args),
        timeout=Time.seconds(args.async_io_timeout_seconds),
        capacity=postgres_async_capacity(args),
        output_type=Types.STRING(),
    ).name("postgres-late-events-dlq").print()


def _attach_feature_sinks(
    env: Any,
    args: StreamConfig,
    *,
    feature_events: Any,
    user_updates: Any,
    item_updates: Any,
    quality_rows: Any,
    late_events: Any,
):
    from pyflink.common import Time, Types
    from pyflink.datastream import AsyncDataStream

    feature_updates = user_updates.union(item_updates)
    if args.disable_online_store:
        emit_progress(
            {
                "status": "online_store_disabled",
                "topic": args.topic,
                "group_id": args.group_id,
            }
        )
        sink_updates = feature_updates
    else:
        sink_updates = AsyncDataStream.unordered_wait(
            data_stream=feature_updates,
            async_function=AsyncRedisFeatureWriter(args),
            timeout=Time.seconds(args.async_io_timeout_seconds),
            capacity=args.async_io_capacity,
            output_type=Types.PICKLED_BYTE_ARRAY(),
        ).name("redis-online-feature-writer")

    if not args.offline_store_enabled:
        return None
    if args.offline_store_sink == "postgres":
        AsyncDataStream.unordered_wait(
            data_stream=sink_updates,
            async_function=AsyncPostgresFeastOfflineWriter(args),
            timeout=Time.seconds(args.async_io_timeout_seconds),
            capacity=postgres_async_capacity(args),
            output_type=Types.STRING(),
        ).name("postgres-feast-offline-feature-writer").print()
        return None
    return build_iceberg_statement_set(
        env,
        args,
        feature_events=feature_events,
        user_updates=user_updates,
        item_updates=item_updates,
        quality_rows=quality_rows,
        late_events=late_events,
    )


def build_realtime_stream(env: Any, args: StreamConfig):
    """Compose source, policies, event-time feature windows, and async sinks."""
    from pyflink.common import Types

    raw_stream = env.from_source(
        build_kafka_source(args),
        build_watermark_strategy(args, EventTimestampAssigner()),
        "cdc-behavior-events-source",
    )
    parsed = raw_stream.map(
        ParseNormalizeEvent(),
        output_type=Types.PICKLED_BYTE_ARRAY(),
    ).filter(KeepValidEvents())
    if not args.continuous and args.max_events > 0:
        parsed = parsed.key_by(lambda event: "native-bounded-limit").process(
            LimitEvents(args),
            output_type=Types.PICKLED_BYTE_ARRAY(),
        )

    deduped = parsed.key_by(lambda event: str(event["event_id"])).process(
        MarkDuplicateEvents(args),
        output_type=Types.PICKLED_BYTE_ARRAY(),
    )
    marked = (
        deduped.key_by(lambda event: str(event["event_id"]))
        .process(
            MarkEventTimeStatus(args),
            output_type=Types.PICKLED_BYTE_ARRAY(),
        )
        .name("watermark-lateness-classifier")
    )
    quality_rows, late_events = build_quality_window_streams(marked, args)
    _attach_postgres_dlq(late_events, args)

    feature_events = marked.filter(KeepFeatureEvents(args)).name(
        "watermark-late-event-policy"
    )
    user_updates, item_updates = build_feature_update_streams(feature_events, args)
    return _attach_feature_sinks(
        env,
        args,
        feature_events=feature_events,
        user_updates=user_updates,
        item_updates=item_updates,
        quality_rows=quality_rows,
        late_events=late_events,
    )


def run_pyflink_stream(args: StreamConfig) -> None:
    from pyflink.datastream import StreamExecutionEnvironment

    env = StreamExecutionEnvironment.get_execution_environment()
    env.set_parallelism(args.parallelism)
    configure_checkpointing(env, args)
    statement_set = build_realtime_stream(env, args)
    if statement_set is None:
        env.execute(f"recsys-native-pyflink-realtime-features-online-{args.group_id}")
    else:
        statement_set.execute().wait()


def _lineage_recorders(args: StreamConfig) -> list[RuntimeLineageRecorder]:
    offline_outputs: set[str] = set()
    if args.offline_store_enabled:
        configured_outputs = (
            POSTGRES_FEATURE_URNS
            if args.offline_store_sink == "postgres"
            else ICEBERG_FEATURE_URNS
        )
        offline_outputs.update(
            configured_outputs[table] for table in REDIS_FEATURE_URNS
        )

    run_id = lineage_run_id()
    recorders = []
    if args.offline_store_enabled:
        recorders.append(
            RuntimeLineageRecorder(
                "STREAMING_FEATURES",
                "run_flink_stream_to_offline_store",
                inputs={KAFKA_TOPIC_URNS["behavior_events"]},
                outputs=offline_outputs,
                run_id=run_id,
            )
        )
    if not args.disable_online_store:
        recorders.append(
            RuntimeLineageRecorder(
                "STREAMING_FEATURES",
                "run_flink_stream_to_online_store",
                inputs={KAFKA_TOPIC_URNS["behavior_events"]},
                outputs=set(REDIS_FEATURE_URNS.values()),
                run_id=run_id,
            )
        )
    return recorders


def main() -> int:
    args = parse_stream_args()
    recorders = _lineage_recorders(args)
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
