from __future__ import annotations

from datetime import date, datetime

from config import DriftConfig


class DriftController:
    def __init__(self, config: DriftConfig):
        self.config = config

    @property
    def scenario(self) -> str | None:
        return self.config.scenario if self.config.enabled else None

    def get_factor(self, value: date | datetime) -> float:
        current_date = value.date() if isinstance(value, datetime) else value
        start_date = self.config.drift_start_date
        if not self.config.enabled or start_date is None or current_date < start_date:
            return 1.0
        if self.config.drift_mode == "abrupt":
            return self.config.purchase_probability_multiplier

        days_after_start = (current_date - start_date).days
        progress = min(days_after_start / self.config.ramp_up_days, 1.0)
        factor = 1.0 + progress * (
            self.config.purchase_probability_multiplier - 1.0
        )
        return round(factor, 8)

    def get_phase(self, value: date | datetime) -> str:
        current_date = value.date() if isinstance(value, datetime) else value
        if not self.config.enabled:
            return "disabled"
        if (
            self.config.baseline_start_date is not None
            and self.config.baseline_end_date is not None
            and self.config.baseline_start_date
            <= current_date
            <= self.config.baseline_end_date
        ):
            return "baseline"
        if (
            self.config.drift_start_date is not None
            and current_date >= self.config.drift_start_date
        ):
            return "post_drift"
        return "pre_drift"
