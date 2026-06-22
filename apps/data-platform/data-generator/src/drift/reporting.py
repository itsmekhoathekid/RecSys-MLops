from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from config import GeneratorConfig
from domain import GeneratedData
from drift.controller import DriftController


FEATURE_VERSION = "user_daily_features_v1"
FEATURE_NAMES = (
    "f_user_purchase_count_90d",
    "f_user_total_orders_90d",
    "f_user_interaction_count_90d",
)

USER_DAILY_FEATURE_SCHEMA = pa.schema(
    [
        ("user_id", pa.int64()),
        ("feature_date", pa.date32()),
        ("f_user_purchase_count_90d", pa.int64()),
        ("f_user_total_orders_90d", pa.int64()),
        ("f_user_interaction_count_90d", pa.int64()),
        ("created_ts", pa.timestamp("us", tz="UTC")),
        ("feature_version", pa.string()),
    ]
)

FEATURE_HEALTH_SCHEMA = pa.schema(
    [
        ("date", pa.date32()),
        ("feature_name", pa.string()),
        ("mean", pa.float64()),
        ("stddev", pa.float64()),
        ("psi_vs_baseline", pa.float64()),
        ("drift_status", pa.string()),
        ("drift_factor", pa.float64()),
        ("created_ts", pa.timestamp("us", tz="UTC")),
    ]
)

DRIFT_ALERT_SCHEMA = pa.schema(
    [
        ("alert_date", pa.date32()),
        ("feature_name", pa.string()),
        ("psi_value", pa.float64()),
        ("drift_factor", pa.float64()),
        ("action", pa.string()),
        ("created_ts", pa.timestamp("us", tz="UTC")),
    ]
)


@dataclass(frozen=True)
class DriftArtifacts:
    paths: dict[str, str]
    summary: dict[str, Any]


def calculate_psi(
    expected: np.ndarray | list[float],
    actual: np.ndarray | list[float],
    buckets: int = 10,
) -> float:
    expected_array = np.asarray(expected, dtype=np.float64)
    actual_array = np.asarray(actual, dtype=np.float64)
    expected_array = expected_array[np.isfinite(expected_array)]
    actual_array = actual_array[np.isfinite(actual_array)]
    if expected_array.size == 0 or actual_array.size == 0:
        return 0.0
    if np.array_equal(np.sort(expected_array), np.sort(actual_array)):
        return 0.0

    quantiles = np.quantile(expected_array, np.linspace(0, 1, buckets + 1))
    internal = np.unique(quantiles[1:-1])
    minimum = float(expected_array.min())
    maximum = float(expected_array.max())
    internal = internal[(internal > minimum) & (internal < maximum)]

    if internal.size == 0:
        combined_min = float(min(expected_array.min(), actual_array.min()))
        combined_max = float(max(expected_array.max(), actual_array.max()))
        if combined_min == combined_max:
            return 0.0
        internal = np.asarray([(combined_min + combined_max) / 2.0])

    edges = np.concatenate(([-np.inf], internal, [np.inf]))
    expected_counts, _ = np.histogram(expected_array, bins=edges)
    actual_counts, _ = np.histogram(actual_array, bins=edges)
    epsilon = 1e-4
    expected_pct = np.maximum(expected_counts / expected_counts.sum(), epsilon)
    actual_pct = np.maximum(actual_counts / actual_counts.sum(), epsilon)
    psi = np.sum(
        (actual_pct - expected_pct) * np.log(actual_pct / expected_pct)
    )
    return float(psi)


def classify_drift(psi: float, is_baseline: bool = False) -> str:
    if is_baseline:
        return "baseline"
    if psi < 0.05:
        return "stable"
    if psi < 0.10:
        return "detected"
    if psi < 0.15:
        return "strong"
    return "alert"


class DriftReporter:
    def __init__(self, config: GeneratorConfig):
        self.config = config
        self.controller = DriftController(config.drift)

    def write(self, run_path: Path, data: GeneratedData) -> DriftArtifacts:
        feature_rows, feature_values = self._build_daily_features(data)
        health_rows = self._build_health_rows(feature_values)
        alert_rows = [
            {
                "alert_date": row["date"],
                "feature_name": row["feature_name"],
                "psi_value": row["psi_vs_baseline"],
                "drift_factor": row["drift_factor"],
                "action": "Investigate user purchase frequency drift",
                "created_ts": row["created_ts"],
            }
            for row in health_rows
            if row["psi_vs_baseline"] >= self.config.drift.psi_alert_threshold
            and row["drift_status"] != "baseline"
            and self.config.drift.drift_start_date is not None
            and row["date"] >= self.config.drift.drift_start_date
        ]

        reports_path = run_path / "reports"
        monitoring_path = run_path / "monitoring"
        reports_path.mkdir(parents=True, exist_ok=True)
        monitoring_path.mkdir(parents=True, exist_ok=True)

        user_features_path = reports_path / "user_daily_features.parquet"
        health_path = monitoring_path / "agg_feature_health_daily.parquet"
        alerts_path = monitoring_path / "feature_drift_alerts.parquet"
        csv_path = reports_path / "drift_validation_report.csv"

        pq.write_table(
            pa.Table.from_pylist(feature_rows, schema=USER_DAILY_FEATURE_SCHEMA),
            user_features_path,
            compression="zstd",
        )
        pq.write_table(
            pa.Table.from_pylist(health_rows, schema=FEATURE_HEALTH_SCHEMA),
            health_path,
            compression="zstd",
        )
        pq.write_table(
            pa.Table.from_pylist(alert_rows, schema=DRIFT_ALERT_SCHEMA),
            alerts_path,
            compression="zstd",
        )
        self._write_csv(csv_path, health_rows)

        summary = self._build_summary(data, health_rows, alert_rows)
        return DriftArtifacts(
            paths={
                "user_daily_features": str(user_features_path),
                "drift_validation_report": str(csv_path),
                "agg_feature_health_daily": str(health_path),
                "feature_drift_alerts": str(alerts_path),
            },
            summary=summary,
        )

    def _build_daily_features(
        self, data: GeneratedData
    ) -> tuple[list[dict[str, Any]], dict[str, dict[date, np.ndarray]]]:
        active_users = sorted(user.user_id for user in data.users if user.is_active)
        user_index = {user_id: index for index, user_id in enumerate(active_users)}
        dates = [
            self.config.history_start_date + timedelta(days=offset)
            for offset in range(self.config.history_days)
        ]
        day_index = {value: index for index, value in enumerate(dates)}
        shape = (len(active_users), len(dates))
        purchases = np.zeros(shape, dtype=np.int64)
        orders = np.zeros(shape, dtype=np.int64)
        interactions = np.zeros(shape, dtype=np.int64)

        canonical_events = {}
        for event in data.behavior_events:
            canonical_events.setdefault(event.event_id, event)
        for event in canonical_events.values():
            row = user_index.get(event.user_id)
            column = day_index.get(event.event_timestamp.date())
            if row is None or column is None:
                continue
            interactions[row, column] += 1
            if event.event_type == "purchase":
                purchases[row, column] += 1

        for order in data.orders:
            row = user_index.get(order.user_id)
            column = day_index.get(order.order_timestamp.date())
            if row is not None and column is not None:
                orders[row, column] += 1

        rolling = {
            "f_user_purchase_count_90d": self._rolling_sum(purchases, 90),
            "f_user_total_orders_90d": self._rolling_sum(orders, 90),
            "f_user_interaction_count_90d": self._rolling_sum(interactions, 90),
        }
        created_ts = datetime.combine(
            dates[-1] + timedelta(days=1), time.min, tzinfo=timezone.utc
        )
        rows: list[dict[str, Any]] = []
        values_by_feature: dict[str, dict[date, np.ndarray]] = {
            feature_name: {} for feature_name in FEATURE_NAMES
        }
        for column, feature_date in enumerate(dates):
            for feature_name in FEATURE_NAMES:
                values_by_feature[feature_name][feature_date] = rolling[feature_name][
                    :, column
                ]
            for row, user_id in enumerate(active_users):
                rows.append(
                    {
                        "user_id": user_id,
                        "feature_date": feature_date,
                        "f_user_purchase_count_90d": int(
                            rolling["f_user_purchase_count_90d"][row, column]
                        ),
                        "f_user_total_orders_90d": int(
                            rolling["f_user_total_orders_90d"][row, column]
                        ),
                        "f_user_interaction_count_90d": int(
                            rolling["f_user_interaction_count_90d"][row, column]
                        ),
                        "created_ts": created_ts,
                        "feature_version": FEATURE_VERSION,
                    }
                )
        return rows, values_by_feature

    @staticmethod
    def _rolling_sum(values: np.ndarray, window: int) -> np.ndarray:
        cumulative = np.cumsum(values, axis=1)
        output = cumulative.copy()
        if values.shape[1] > window:
            output[:, window:] = cumulative[:, window:] - cumulative[:, :-window]
        return output

    def _build_health_rows(
        self, values_by_feature: dict[str, dict[date, np.ndarray]]
    ) -> list[dict[str, Any]]:
        drift = self.config.drift
        assert drift.baseline_start_date is not None
        assert drift.baseline_end_date is not None
        created_ts = datetime.combine(
            self.config.history_start_date
            + timedelta(days=self.config.history_days),
            time.min,
            tzinfo=timezone.utc,
        )
        rows: list[dict[str, Any]] = []
        for feature_name, daily_values in values_by_feature.items():
            baseline_values = np.concatenate(
                [
                    values
                    for feature_date, values in daily_values.items()
                    if drift.baseline_start_date
                    <= feature_date
                    <= drift.baseline_end_date
                ]
            )
            for feature_date, values in daily_values.items():
                is_baseline = (
                    drift.baseline_start_date
                    <= feature_date
                    <= drift.baseline_end_date
                )
                psi = calculate_psi(baseline_values, values)
                rows.append(
                    {
                        "date": feature_date,
                        "feature_name": feature_name,
                        "mean": float(np.mean(values)),
                        "stddev": float(np.std(values)),
                        "psi_vs_baseline": psi,
                        "drift_status": classify_drift(psi, is_baseline=is_baseline),
                        "drift_factor": self.controller.get_factor(feature_date),
                        "created_ts": created_ts,
                    }
                )
        rows.sort(key=lambda row: (row["date"], row["feature_name"]))
        return rows

    def _build_summary(
        self,
        data: GeneratedData,
        health_rows: list[dict[str, Any]],
        alert_rows: list[dict[str, Any]],
    ) -> dict[str, Any]:
        drift = self.config.drift
        assert drift.baseline_start_date is not None
        assert drift.baseline_end_date is not None
        assert drift.drift_start_date is not None
        ramp_end = drift.drift_start_date + timedelta(days=drift.ramp_up_days)

        canonical_events = {}
        for event in data.behavior_events:
            canonical_events.setdefault(event.event_id, event)

        def conversion_rate(start: date, end: date | None) -> float:
            relevant = [
                event
                for event in canonical_events.values()
                if event.event_timestamp.date() >= start
                and (end is None or event.event_timestamp.date() <= end)
            ]
            carts = sum(event.event_type == "cart" for event in relevant)
            purchases = sum(event.event_type == "purchase" for event in relevant)
            return purchases / carts if carts else 0.0

        factors = [event.drift_factor for event in canonical_events.values()]
        post_drift_health = [
            row
            for row in health_rows
            if row["date"] >= drift.drift_start_date
        ]
        return {
            "scenario": drift.scenario,
            "baseline_purchase_rate": conversion_rate(
                drift.baseline_start_date, drift.baseline_end_date
            ),
            "post_ramp_purchase_rate": conversion_rate(ramp_end, None),
            "drift_factor_min": min(factors, default=1.0),
            "drift_factor_max": max(factors, default=1.0),
            "max_psi": max(
                (row["psi_vs_baseline"] for row in post_drift_health), default=0.0
            ),
            "alert_count": len(alert_rows),
        }

    @staticmethod
    def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
        columns = [
            "date",
            "feature_name",
            "mean",
            "stddev",
            "psi_vs_baseline",
            "drift_status",
            "drift_factor",
        ]
        with path.open("w", encoding="utf-8", newline="") as file:
            writer = csv.DictWriter(file, fieldnames=columns, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
