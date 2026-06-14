from datetime import date, datetime, timezone
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq

from data_generator.behavior import BehaviorContext, BehaviorProbabilityModel
from data_generator.config import DriftConfig, load_config
from data_generator.drift.controller import DriftController
from data_generator.drift.reporting import (
    DriftReporter,
    calculate_psi,
    classify_drift,
)
from data_generator.pipeline import HistoricalDataPipeline
from data_generator.simulation import RecsysSimulation
from data_generator.validation import validate_drift_output


def test_drift_controller_disabled_and_pre_drift():
    disabled = DriftController(DriftConfig())
    assert disabled.get_factor(date(2025, 8, 1)) == 1.0
    assert disabled.get_phase(date(2025, 8, 1)) == "disabled"

    config = DriftConfig(
        enabled=True,
        drift_start_date=date(2025, 7, 30),
        drift_mode="gradual",
        purchase_probability_multiplier=1.5,
        ramp_up_days=30,
        baseline_start_date=date(2025, 6, 30),
        baseline_end_date=date(2025, 7, 29),
    )
    controller = DriftController(config)
    assert controller.get_factor(date(2025, 7, 29)) == 1.0
    assert controller.get_phase(date(2025, 7, 1)) == "baseline"
    assert controller.get_phase(date(2025, 6, 1)) == "pre_drift"
    assert controller.get_phase(date(2025, 7, 30)) == "post_drift"


def test_drift_controller_abrupt_and_gradual_boundaries():
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
    assert abrupt.get_factor(date(2025, 7, 30)) == 1.5

    gradual = DriftController(
        abrupt.config.model_copy(update={"drift_mode": "gradual", "ramp_up_days": 30})
    )
    assert gradual.get_factor(date(2025, 7, 30)) == 1.0
    assert gradual.get_factor(date(2025, 8, 14)) == 1.25
    assert gradual.get_factor(date(2025, 8, 29)) == 1.5
    assert gradual.get_factor(date(2025, 9, 20)) == 1.5


def test_purchase_probability_applies_factor_and_clamps(small_config):
    simulation = RecsysSimulation(small_config)
    users, _ = simulation._generate_users()
    products, _ = simulation._generate_products()
    model = BehaviorProbabilityModel(
        small_config.session_behavior.model_copy(
            update={"purchase_after_cart_base": 0.9}
        )
    )
    context = BehaviorContext(
        rank_position=1, is_campaign=True, drift_factor=10.0
    )
    assert model.p_purchase(users[0], products[0], context) == 0.95


def test_psi_and_status_contracts():
    baseline = np.asarray([0, 0, 0, 1, 1, 2, 2, 3], dtype=float)
    assert calculate_psi(baseline, baseline) == 0.0
    shifted = calculate_psi(baseline, np.asarray([5, 5, 6, 6, 7, 7]))
    assert shifted > 0.15
    assert calculate_psi(np.zeros(100), np.zeros(100)) == 0.0
    assert calculate_psi(np.zeros(100), np.ones(100)) > 0
    assert classify_drift(0.01) == "stable"
    assert classify_drift(0.07) == "detected"
    assert classify_drift(0.12) == "strong"
    assert classify_drift(0.15) == "alert"
    assert classify_drift(10, is_baseline=True) == "baseline"


def test_rolling_window_excludes_future_and_old_values():
    values = np.zeros((1, 100), dtype=np.int64)
    values[0, 0] = 2
    values[0, 10] = 3
    values[0, 99] = 5
    rolling = DriftReporter._rolling_sum(values, 90)
    assert rolling[0, 9] == 2
    assert rolling[0, 10] == 5
    assert rolling[0, 89] == 5
    assert rolling[0, 90] == 3
    assert rolling[0, 98] == 3
    assert rolling[0, 99] == 8


def test_drift_metadata_and_artifacts(tmp_path):
    config = load_config(Path("config/data_generator_drift.yaml"))
    config = config.model_copy(
        update={
            "entities": config.entities.model_copy(
                update={
                    "n_users": 80,
                    "n_products": 80,
                    "n_categories": 10,
                    "n_brands": 20,
                }
            ),
            "traffic": config.traffic.model_copy(
                update={"target_behavior_events": 3000, "target_tolerance": 0.05}
            ),
            "drift": config.drift.model_copy(
                update={"purchase_probability_multiplier": 3.0}
            ),
            "output": config.output.model_copy(
                update={
                    "base_path": str(tmp_path),
                    "run_id": "drift-small",
                    "overwrite": True,
                }
            ),
        }
    )
    result = HistoricalDataPipeline(config).run()
    run_path = Path(result["run_path"])
    validation = validate_drift_output(run_path, config)
    assert validation.passed, validation.errors

    events = pq.read_table(
        sorted((run_path / "behavior_events").rglob("*.parquet"))[0]
    ).to_pylist()
    controller = DriftController(config.drift)
    assert all(
        event["drift_factor"]
        == controller.get_factor(event["event_timestamp"])
        for event in events
    )
    assert (run_path / "reports/drift_validation_report.csv").exists()
    assert (run_path / "reports/user_daily_features.parquet").exists()
    alerts = pq.read_table(
        run_path / "monitoring/feature_drift_alerts.parquet"
    ).to_pylist()
    assert all(
        row["alert_date"] >= config.drift.drift_start_date for row in alerts
    )
    assert (
        result["data_quality_report"]["drift"]["post_ramp_purchase_rate"]
        > result["data_quality_report"]["drift"]["baseline_purchase_rate"]
    )


def test_drift_report_is_reproducible(tmp_path):
    config = load_config(Path("config/data_generator_drift.yaml"))
    compact = config.model_copy(
        update={
            "entities": config.entities.model_copy(
                update={
                    "n_users": 40,
                    "n_products": 50,
                    "n_categories": 8,
                    "n_brands": 15,
                }
            ),
            "traffic": config.traffic.model_copy(
                update={"target_behavior_events": 1000, "target_tolerance": 0.1}
            ),
            "output": config.output.model_copy(
                update={"base_path": str(tmp_path), "run_id": "first"}
            ),
        }
    )
    first = HistoricalDataPipeline(compact).run()
    second_config = compact.model_copy(
        update={
            "output": compact.output.model_copy(update={"run_id": "second"})
        }
    )
    second = HistoricalDataPipeline(second_config).run()
    first_csv = (
        Path(first["run_path"]) / "reports/drift_validation_report.csv"
    ).read_text()
    second_csv = (
        Path(second["run_path"]) / "reports/drift_validation_report.csv"
    ).read_text()
    assert first_csv == second_csv
