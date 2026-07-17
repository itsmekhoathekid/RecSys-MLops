from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from features.flink.time_utils import parse_event_time


class LateArrivalMetricCounters:
    """Flink counters for watermark-based late-arrival classification."""

    def __init__(self, metric_group: Any):
        self.late_arrivals_total = metric_group.counter("late_arrivals_total")
        self.accepted_late_events_total = metric_group.counter("accepted_late_events_total")
        self.too_late_events_total = metric_group.counter("too_late_events_total")

    @classmethod
    def from_runtime_context(cls, runtime_context: Any) -> "LateArrivalMetricCounters":
        return cls(runtime_context.get_metrics_group())

    def record(self, is_late: bool, is_too_late: bool) -> None:
        if not is_late:
            return
        self.late_arrivals_total.inc()
        if is_too_late:
            self.too_late_events_total.inc()
        else:
            self.accepted_late_events_total.inc()


def late_arrival_metrics(event: dict[str, Any], allowed_lateness_seconds: int) -> tuple[float, bool]:
    """Return native watermark markers, with a wall-clock fallback for non-Flink callers."""
    if "_late_by_seconds" in event or "_is_late" in event:
        return float(event.get("_late_by_seconds") or 0.0), bool(event.get("_is_late"))
    processed_ts = datetime.now(timezone.utc)
    event_ts = parse_event_time(event["event_timestamp"])
    late_by_seconds = max(0.0, float((processed_ts - event_ts).total_seconds()))
    return late_by_seconds, late_by_seconds > allowed_lateness_seconds


def event_time_status(
    event: dict[str, Any],
    current_watermark_ms: int,
    allowed_lateness_seconds: int,
    quality_window_seconds: int,
) -> tuple[float, bool, bool]:
    """Classify one event against Flink's current event-time watermark."""
    if current_watermark_ms <= -(2**62):
        return 0.0, False, False
    event_ms = int(parse_event_time(event["event_timestamp"]).timestamp() * 1000)
    window_ms = max(1, int(quality_window_seconds)) * 1000
    window_end_ms = ((event_ms // window_ms) + 1) * window_ms
    late_by_seconds = max(0.0, float(current_watermark_ms - event_ms) / 1000.0)
    is_late = event_ms <= current_watermark_ms
    is_too_late = current_watermark_ms >= window_end_ms + max(0, allowed_lateness_seconds) * 1000
    return late_by_seconds, is_late, is_too_late
