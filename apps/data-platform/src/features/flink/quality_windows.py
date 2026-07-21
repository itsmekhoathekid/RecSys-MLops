from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from features.flink.time_utils import parse_event_time


def native_quality_window_aggregate(args: Any):
    """Build an incremental PyFlink aggregate without importing PyFlink in unit-test processes."""
    from pyflink.datastream.functions import AggregateFunction

    class NativeQualityWindowAggregate(AggregateFunction):
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
                event_seconds % args.quality_window_seconds
            )
            accumulator["event_count"] += 1
            accumulator["late_event_count"] += int(bool(event.get("_is_late")))
            accumulator["duplicate_event_count"] += int(
                bool(event.get("_is_duplicate"))
            )
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
                + timedelta(seconds=args.quality_window_seconds),
                "topic": args.topic,
                "event_count": event_count,
                "late_event_count": int(accumulator["late_event_count"]),
                "late_events_dropped": 0,
                "side_output_late_events": 0,
                "duplicate_event_count": int(accumulator["duplicate_event_count"]),
                "max_late_by_seconds": float(accumulator["max_late_by_seconds"]),
                "is_bursty": event_count >= args.burst_threshold_event_count,
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

    return NativeQualityWindowAggregate()
