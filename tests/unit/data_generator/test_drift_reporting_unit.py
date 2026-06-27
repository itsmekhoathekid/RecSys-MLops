from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import numpy as np
import pyarrow.parquet as pq

from config import DriftConfig, load_config
from drift.controller import DriftController
from drift.reporting import DriftReporter, calculate_psi, classify_drift


def test_drift_controller_modes_phase_and_scenario():
    disabled = DriftController(DriftConfig())
    assert disabled.scenario is None
    assert disabled.get_factor(datetime(2025, 8, 1, tzinfo=timezone.utc)) == 1.0
    assert disabled.get_phase(date(2025, 8, 1)) == "disabled"

    abrupt = DriftController(
        DriftConfig(
            enabled=True,
            drift_start_date=date(2025, 7, 30),
            drift_mode="abrupt",
            purchase_probability_multiplier=1.5,
            baseline_start_date=date(2025, 6, 30),
            baseline_end_date=date(2025, 7, 29),
        )
    )
    assert abrupt.scenario == "user_purchase_frequency"
    assert abrupt.get_factor(date(2025, 7, 29)) == 1.0
    assert abrupt.get_factor(date(2025, 7, 30)) == 1.5
    assert abrupt.get_phase(date(2025, 7, 1)) == "baseline"
    assert abrupt.get_phase(date(2025, 6, 1)) == "pre_drift"
    assert abrupt.get_phase(date(2025, 7, 30)) == "post_drift"

    gradual = DriftController(
        abrupt.config.model_copy(update={"drift_mode": "gradual", "ramp_up_days": 30})
    )
    assert gradual.get_factor(date(2025, 7, 30)) == 1.0
    assert gradual.get_factor(date(2025, 8, 14)) == 1.25
    assert gradual.get_factor(date(2025, 8, 29)) == 1.5
    assert gradual.get_factor(date(2025, 9, 20)) == 1.5


def test_drift_reporting_psi_and_status_contracts():
    baseline = np.asarray([0, 0, 0, 1, 1, 2, 2, 3], dtype=float)

    assert calculate_psi([], [1, 2, 3]) == 0.0
    assert calculate_psi(baseline, baseline) == 0.0
    assert calculate_psi([1, 1, np.nan], [1, 1, np.inf]) == 0.0
    assert calculate_psi(np.zeros(100), np.zeros(100)) == 0.0
    assert calculate_psi(np.zeros(100), np.ones(100)) > 0
    assert calculate_psi(baseline, np.asarray([5, 5, 6, 6, 7, 7])) > 0.15
    assert classify_drift(0.01) == "stable"
    assert classify_drift(0.07) == "detected"
    assert classify_drift(0.12) == "strong"
    assert classify_drift(0.15) == "alert"
    assert classify_drift(10, is_baseline=True) == "baseline"


def test_drift_reporter_writes_artifacts_and_summary(tmp_path):
    config = load_config(Path("configs/local/data_generator_drift.yaml"))
    config = config.model_copy(
        update={
            "drift": config.drift.model_copy(update={"psi_alert_threshold": 0.001})
        }
    )
    reporter = DriftReporter(config)
    baseline_day = datetime(2025, 7, 1, 12, tzinfo=timezone.utc)
    post_ramp_day = datetime(2025, 9, 5, 12, tzinfo=timezone.utc)

    def user(user_id: int, active: bool = True):
        return SimpleNamespace(user_id=user_id, is_active=active)

    def event(event_type: str, ts: datetime, user_id: int, drift_factor: float):
        return SimpleNamespace(
            event_id=uuid4(),
            event_timestamp=ts,
            event_type=event_type,
            user_id=user_id,
            drift_factor=drift_factor,
        )

    events = [
        event("cart", baseline_day, 1, 1.0),
        event("purchase", baseline_day, 1, 1.0),
        event("cart", baseline_day, 2, 1.0),
        event("cart", post_ramp_day, 1, 1.5),
        event("purchase", post_ramp_day, 1, 1.5),
        event("purchase", post_ramp_day, 1, 1.5),
        event("purchase", post_ramp_day, 2, 1.5),
    ]
    data = SimpleNamespace(
        users=[user(1), user(2), user(3, active=False)],
        behavior_events=events,
        orders=[
            SimpleNamespace(user_id=1, order_timestamp=baseline_day),
            SimpleNamespace(user_id=1, order_timestamp=post_ramp_day),
        ],
    )

    artifacts = reporter.write(tmp_path, data)

    assert set(artifacts.paths) == {
        "user_daily_features",
        "drift_validation_report",
        "agg_feature_health_daily",
        "feature_drift_alerts",
    }
    assert artifacts.summary["scenario"] == "user_purchase_frequency"
    assert artifacts.summary["baseline_purchase_rate"] == 0.5
    assert artifacts.summary["post_ramp_purchase_rate"] == 3.0
    assert artifacts.summary["drift_factor_min"] == 1.0
    assert artifacts.summary["drift_factor_max"] == 1.5
    assert artifacts.summary["alert_count"] >= 1

    user_features = pq.read_table(artifacts.paths["user_daily_features"]).to_pylist()
    assert len(user_features) == config.history_days * 2
    assert all(row["feature_version"] == "user_daily_features_v1" for row in user_features)

    health_rows = pq.read_table(artifacts.paths["agg_feature_health_daily"]).to_pylist()
    assert any(row["drift_status"] == "baseline" for row in health_rows)
    post_drift_psi = [
        row["psi_vs_baseline"]
        for row in health_rows
        if row["date"] >= config.drift.drift_start_date
    ]
    assert max(post_drift_psi) == artifacts.summary["max_psi"]

    alerts = pq.read_table(artifacts.paths["feature_drift_alerts"]).to_pylist()
    assert alerts
    assert all(row["alert_date"] >= config.drift.drift_start_date for row in alerts)
    assert "psi_vs_baseline" in Path(artifacts.paths["drift_validation_report"]).read_text()
