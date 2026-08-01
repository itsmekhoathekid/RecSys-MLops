from __future__ import annotations

from typing import Any

from features.flink.event_time import LateArrivalMetricCounters, event_time_status
from features.flink.pyflink_compat import FilterFunction, KeyedProcessFunction


class MarkEventTimeStatus(KeyedProcessFunction):
    def __init__(self, args: Any) -> None:
        self.args = args

    def open(self, runtime_context):
        self.late_arrival_metrics = LateArrivalMetricCounters.from_runtime_context(
            runtime_context
        )

    def process_element(self, event: dict[str, Any], ctx):
        watermark_ms = int(ctx.timer_service().current_watermark())
        late_by_seconds, is_late, is_too_late = event_time_status(
            event,
            watermark_ms,
            self.args.allowed_lateness_seconds,
            self.args.feature_window_seconds,
        )
        self.late_arrival_metrics.record(is_late, is_too_late)
        marked = dict(event)
        marked["_late_by_seconds"] = late_by_seconds
        marked["_is_late"] = is_late
        marked["_is_too_late"] = is_too_late
        yield marked


class KeepFeatureEvents(FilterFunction):
    def __init__(self, args: Any) -> None:
        self.args = args

    def filter(self, event: dict[str, Any]) -> bool:
        if event.get("_is_duplicate"):
            return False
        return not (self.args.drop_late_events and event.get("_is_too_late"))
