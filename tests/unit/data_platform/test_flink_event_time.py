from datetime import datetime, timezone

from features.flink.event_time import event_time_status


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
