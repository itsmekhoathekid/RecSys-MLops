from datetime import datetime, timezone

from features.flink.event_time import LateArrivalMetricCounters, event_time_status


class FakeCounter:
    def __init__(self):
        self.count = 0

    def inc(self, value: int = 1):
        self.count += value

    def dec(self, value: int = 1):
        self.count -= value


class FakeMetricGroup:
    def __init__(self):
        self.counters: dict[str, FakeCounter] = {}

    def counter(self, name: str) -> FakeCounter:
        self.counters[name] = FakeCounter()
        return self.counters[name]


class FakeRuntimeContext:
    def __init__(self):
        self.metric_group = FakeMetricGroup()

    def get_metrics_group(self) -> FakeMetricGroup:
        return self.metric_group


def _watermark_ms(value: str) -> int:
    return int(datetime.fromisoformat(value).replace(tzinfo=timezone.utc).timestamp() * 1000)


def test_event_time_status_uses_window_cleanup_boundary():
    event = {"event_timestamp": "2026-07-17T00:00:00+00:00"}

    late_by, is_late, is_too_late = event_time_status(
        event,
        _watermark_ms("2026-07-17T00:00:30"),
        allowed_lateness_seconds=300,
        quality_window_seconds=60,
    )
    assert late_by == 30.0
    assert is_late is True
    assert is_too_late is False

    _, _, is_too_late = event_time_status(
        event,
        _watermark_ms("2026-07-17T00:06:00"),
        allowed_lateness_seconds=300,
        quality_window_seconds=60,
    )
    assert is_too_late is True


def test_event_time_status_accepts_events_before_first_watermark():
    assert event_time_status(
        {"event_timestamp": "2026-07-17T00:00:00+00:00"},
        -(2**63),
        allowed_lateness_seconds=300,
        quality_window_seconds=60,
    ) == (0.0, False, False)


def test_late_arrival_metric_counters_partition_late_events():
    runtime_context = FakeRuntimeContext()
    metrics = LateArrivalMetricCounters.from_runtime_context(runtime_context)

    metrics.record(is_late=False, is_too_late=False)
    metrics.record(is_late=True, is_too_late=False)
    metrics.record(is_late=True, is_too_late=True)

    counters = runtime_context.metric_group.counters
    assert set(counters) == {
        "late_arrivals_total",
        "accepted_late_events_total",
        "too_late_events_total",
    }
    assert counters["late_arrivals_total"].count == 2
    assert counters["accepted_late_events_total"].count == 1
    assert counters["too_late_events_total"].count == 1
    assert counters["late_arrivals_total"].count == (
        counters["accepted_late_events_total"].count + counters["too_late_events_total"].count
    )
