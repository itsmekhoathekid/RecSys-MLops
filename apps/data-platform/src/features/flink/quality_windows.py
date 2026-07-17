from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from features.flink.time_utils import parse_event_time


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
    """Small pure-Python tracker retained for unit tests and local diagnostics."""

    def __init__(self, topic: str, window_seconds: int = 60, burst_threshold_event_count: int = 500):
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
        self.current.late_event_count += int(is_late)
        self.current.late_events_dropped += int(is_late and drop_late_events)
        self.current.side_output_late_events += int(is_late)
        self.current.duplicate_event_count += int(is_duplicate)
        self.current.max_late_by_seconds = max(self.current.max_late_by_seconds, late_by_seconds)
        return emitted

    def flush(self) -> list[StreamQualityWindow]:
        if self.current is None:
            return []
        self.current.is_bursty = self.current.event_count >= self.burst_threshold_event_count
        emitted = [self.current]
        self.current = None
        return emitted


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
            accumulator["window_start_seconds"] = event_seconds - (event_seconds % args.quality_window_seconds)
            accumulator["event_count"] += 1
            accumulator["late_event_count"] += int(bool(event.get("_is_late")))
            accumulator["duplicate_event_count"] += int(bool(event.get("_is_duplicate")))
            accumulator["max_late_by_seconds"] = max(
                float(accumulator["max_late_by_seconds"]),
                float(event.get("_late_by_seconds") or 0.0),
            )
            return accumulator

        def get_result(self, accumulator: dict[str, Any]):
            window_start = datetime.fromtimestamp(int(accumulator["window_start_seconds"]), tz=timezone.utc)
            event_count = int(accumulator["event_count"])
            return {
                "window_start": window_start,
                "window_end": window_start + timedelta(seconds=args.quality_window_seconds),
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
                "window_start_seconds": left["window_start_seconds"] or right["window_start_seconds"],
                "event_count": int(left["event_count"]) + int(right["event_count"]),
                "late_event_count": int(left["late_event_count"]) + int(right["late_event_count"]),
                "duplicate_event_count": int(left["duplicate_event_count"]) + int(right["duplicate_event_count"]),
                "max_late_by_seconds": max(
                    float(left["max_late_by_seconds"]),
                    float(right["max_late_by_seconds"]),
                ),
            }

    return NativeQualityWindowAggregate()
