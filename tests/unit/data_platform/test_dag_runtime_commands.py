from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import monitoring.push_drift_report_metrics as drift_metrics
from feature_store.materialize_online import (
    SourceFeatureBounds,
    materialize_with_recovery,
)


class FakeFeatureStore:
    def __init__(self, watermarks):
        self.views = [
            SimpleNamespace(online=True, most_recent_end_time=watermark)
            for watermark in watermarks
        ]
        self.incremental_calls = []
        self.full_calls = []

    def list_feature_views(self):
        return self.views

    def materialize_incremental(self, end):
        self.incremental_calls.append(end)

    def materialize(self, start, end, *, disable_event_timestamp=False):
        self.full_calls.append((start, end, disable_event_timestamp))


def test_drift_report_metric_publisher_uses_importable_runtime(monkeypatch):
    pushed = {}
    monkeypatch.setattr(
        drift_metrics,
        "read_json",
        lambda _path: {"run_id": "drift-42", "passed": False},
    )

    def fake_push(samples, job, *, gateway_url, grouping_key):
        pushed.update(
            samples=samples,
            job=job,
            gateway_url=gateway_url,
            grouping_key=grouping_key,
        )
        return True

    monkeypatch.setattr(drift_metrics, "push_metrics", fake_push)
    result = drift_metrics.publish_drift_report_metrics(
        "s3://bucket/report.json", gateway_url="http://pushgateway"
    )

    assert result == {
        "pushed_drift_report_metrics": True,
        "run_id": "drift-42",
    }
    assert pushed["job"] == "recsys_offline_feature_drift_report"
    assert pushed["grouping_key"] == {"run_id": "drift-42"}
    assert pushed["samples"][0].labels == {
        "run_id": "drift-42",
        "passed": "false",
    }


def test_drift_report_metric_publisher_reports_a_failed_push(monkeypatch):
    monkeypatch.setattr(
        drift_metrics,
        "read_json",
        lambda _path: {"run_id": "drift-43", "passed": True},
    )
    monkeypatch.setattr(drift_metrics, "push_metrics", lambda *args, **kwargs: False)

    assert drift_metrics.publish_drift_report_metrics("report.json") == {
        "pushed_drift_report_metrics": False,
        "run_id": "drift-43",
    }


def test_materialization_repairs_a_registry_watermark_ahead_of_source():
    source_end = datetime(2026, 3, 29, tzinfo=timezone.utc)
    bounds = SourceFeatureBounds(source_end - timedelta(days=14), source_end)
    store = FakeFeatureStore(
        [
            datetime(2026, 8, 19, tzinfo=timezone.utc),
            source_end - timedelta(hours=1),
            None,
        ]
    )

    result = materialize_with_recovery(
        store,
        bounds,
        lambda: {"status": "SUCCESS"},
    )

    assert result.mode == "full_watermark_recovery"
    assert store.incremental_calls == []
    assert store.full_calls == [(bounds.start, bounds.end, True)]


def test_materialization_repairs_an_empty_or_expired_online_store():
    source_end = datetime(2026, 8, 19, tzinfo=timezone.utc)
    bounds = SourceFeatureBounds(source_end - timedelta(days=1), source_end)
    store = FakeFeatureStore([source_end] * 3)
    reports = iter([{"status": "FAILURE"}, {"status": "SUCCESS"}])

    result = materialize_with_recovery(store, bounds, lambda: next(reports))

    assert result.mode == "full_online_store_recovery"
    assert store.full_calls == [(bounds.start, bounds.end, True)]


def test_materialization_uses_incremental_mode_for_new_source_rows():
    source_end = datetime(2026, 8, 19, tzinfo=timezone.utc)
    bounds = SourceFeatureBounds(source_end - timedelta(days=1), source_end)
    store = FakeFeatureStore([source_end - timedelta(hours=2)] * 3)

    result = materialize_with_recovery(
        store,
        bounds,
        lambda: {"status": "SUCCESS"},
    )

    assert result.mode == "incremental"
    assert store.incremental_calls == [source_end]
    assert store.full_calls == []


def test_materialization_fails_if_full_recovery_does_not_validate():
    source_end = datetime(2026, 8, 19, tzinfo=timezone.utc)
    bounds = SourceFeatureBounds(source_end - timedelta(days=1), source_end)
    store = FakeFeatureStore([source_end] * 3)

    with pytest.raises(RuntimeError, match="validation failed after recovery"):
        materialize_with_recovery(
            store,
            bounds,
            lambda: {"status": "FAILURE", "datasets": {}},
        )
