from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from features.flink.pyflink_compat import AggregateFunction
from features.flink.time_utils import parse_event_time


class NativeQualityWindowAggregate(AggregateFunction):
    def __init__(self, args: Any) -> None:
        self.args = args

    def create_accumulator(self):
        return {
            "window_start_seconds": None,
            "event_count": 0,
            "late_event_count": 0,
            "duplicate_event_count": 0,
            "max_late_by_seconds": 0.0,
        }

    def add(self, event: dict[str, Any], accumulator: dict[str, Any]):
        event_seconds = int(parse_event_time(event["event_timestamp"]).timestamp())
        accumulator["window_start_seconds"] = event_seconds - (
            event_seconds % self.args.quality_window_seconds
        )
        accumulator["event_count"] += 1
        accumulator["late_event_count"] += int(bool(event.get("_is_late")))
        accumulator["duplicate_event_count"] += int(bool(event.get("_is_duplicate")))
        accumulator["max_late_by_seconds"] = max(
            float(accumulator["max_late_by_seconds"]),
            float(event.get("_late_by_seconds") or 0.0),
        )
        return accumulator

    def get_result(self, accumulator: dict[str, Any]):
        window_start = datetime.fromtimestamp(
            int(accumulator["window_start_seconds"]), tz=timezone.utc
        )
        event_count = int(accumulator["event_count"])
        return {
            "window_start": window_start,
            "window_end": window_start
            + timedelta(seconds=self.args.quality_window_seconds),
            "topic": self.args.topic,
            "event_count": event_count,
            "late_event_count": int(accumulator["late_event_count"]),
            "late_events_dropped": 0,
            "side_output_late_events": 0,
            "duplicate_event_count": int(accumulator["duplicate_event_count"]),
            "max_late_by_seconds": float(accumulator["max_late_by_seconds"]),
            "is_bursty": event_count >= self.args.burst_threshold_event_count,
            "created_timestamp": datetime.now(timezone.utc),
        }

    def merge(self, left: dict[str, Any], right: dict[str, Any]):
        return {
            "window_start_seconds": left["window_start_seconds"]
            or right["window_start_seconds"],
            "event_count": int(left["event_count"]) + int(right["event_count"]),
            "late_event_count": int(left["late_event_count"])
            + int(right["late_event_count"]),
            "duplicate_event_count": int(left["duplicate_event_count"])
            + int(right["duplicate_event_count"]),
            "max_late_by_seconds": max(
                float(left["max_late_by_seconds"]),
                float(right["max_late_by_seconds"]),
            ),
        }


def build_quality_window_streams(marked_events: Any, args: Any) -> tuple[Any, Any]:
    """Attach the quality window, metrics output, and too-late side output."""
    from pyflink.common import Time, Types
    from pyflink.datastream.output_tag import OutputTag
    from pyflink.datastream.window import TumblingEventTimeWindows

    from features.flink.operators.row_mappers import QualityWindowMetricLog

    late_tag = OutputTag("late-events", Types.PICKLED_BYTE_ARRAY())
    quality_rows = (
        marked_events.key_by(lambda event: args.topic)
        .window(TumblingEventTimeWindows.of(Time.seconds(args.quality_window_seconds)))
        .allowed_lateness(args.allowed_lateness_seconds * 1000)
        .side_output_late_data(late_tag)
        .aggregate(
            NativeQualityWindowAggregate(args),
            accumulator_type=Types.PICKLED_BYTE_ARRAY(),
            output_type=Types.PICKLED_BYTE_ARRAY(),
        )
        .name("native-event-time-quality-windows")
    )
    quality_rows.map(
        QualityWindowMetricLog(args),
        output_type=Types.STRING(),
    ).name("streaming-quality-window-metrics").print()
    return quality_rows, quality_rows.get_side_output(late_tag).name(
        "native-late-events-side-output"
    )
