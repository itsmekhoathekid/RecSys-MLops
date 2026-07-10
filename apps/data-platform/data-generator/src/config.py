from __future__ import annotations

from datetime import date
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, model_validator


class EntityConfig(BaseModel):
    n_users: int = Field(gt=0)
    n_products: int = Field(gt=0)
    n_categories: int = Field(gt=0)
    n_brands: int = Field(gt=0)
    preferences_per_user: int = Field(default=3, gt=0)


class TrafficConfig(BaseModel):
    target_behavior_events: int = Field(gt=0)
    target_tolerance: float = Field(default=0.02, ge=0, le=0.25)
    requests_per_session_min: int = Field(default=1, gt=0)
    requests_per_session_max: int = Field(default=2, gt=0)
    impressions_per_request_min: int = Field(default=5, gt=0)
    impressions_per_request_max: int = Field(default=10, gt=0)
    session_gap_minutes_min: int = Field(default=5, gt=0)
    session_gap_minutes_max: int = Field(default=240, gt=0)

    @model_validator(mode="after")
    def validate_ranges(self) -> "TrafficConfig":
        if self.requests_per_session_min > self.requests_per_session_max:
            raise ValueError("requests_per_session_min must be <= max")
        if self.impressions_per_request_min > self.impressions_per_request_max:
            raise ValueError("impressions_per_request_min must be <= max")
        if self.session_gap_minutes_min > self.session_gap_minutes_max:
            raise ValueError("session_gap_minutes_min must be <= max")
        return self


class SessionBehaviorConfig(BaseModel):
    view_after_impression_base: float = Field(ge=0, le=1)
    cart_after_view_base: float = Field(ge=0, le=1)
    purchase_after_cart_base: float = Field(ge=0, le=1)


class DistributionConfig(BaseModel):
    top_city: str
    top_city_ratio: float = Field(ge=0, le=1)
    cities: list[str]
    top_category_ratio: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def validate_city(self) -> "DistributionConfig":
        if self.top_city not in self.cities:
            raise ValueError("top_city must be present in cities")
        return self


class ChallengeConfig(BaseModel):
    duplicate_event_rate: float = Field(ge=0, le=1)
    conflicting_duplicate_rate: float = Field(ge=0, le=1)
    late_arrival_rate: float = Field(ge=0, le=1)
    out_of_order_rate: float = Field(ge=0, le=1)
    late_delay_minutes_min: int = Field(gt=0)
    late_delay_minutes_max: int = Field(gt=0)

    @model_validator(mode="after")
    def validate_delays(self) -> "ChallengeConfig":
        if self.late_delay_minutes_min > self.late_delay_minutes_max:
            raise ValueError("late delay min must be <= max")
        return self


class BurstWindow(BaseModel):
    start_hour: int = Field(ge=0, le=23)
    end_hour: int = Field(ge=1, le=24)
    traffic_weight: float = Field(gt=0)


class SchemaEvolutionConfig(BaseModel):
    change_date: date
    breaking_change_date: date | None = None
    breaking_schema_version: int = Field(default=3, ge=3)

    @model_validator(mode="after")
    def validate_breaking_change(self) -> "SchemaEvolutionConfig":
        if (
            self.breaking_change_date is not None
            and self.breaking_change_date <= self.change_date
        ):
            raise ValueError("breaking_change_date must be after change_date")
        return self


class DriftConfig(BaseModel):
    enabled: bool = False
    scenario: str = "user_purchase_frequency"
    drift_start_date: date | None = None
    drift_mode: str = "gradual"
    purchase_probability_multiplier: float = Field(default=1.0, ge=1.0)
    ramp_up_days: int = Field(default=30, gt=0)
    baseline_start_date: date | None = None
    baseline_end_date: date | None = None
    psi_alert_threshold: float = Field(default=0.15, gt=0)

    @model_validator(mode="after")
    def validate_drift(self) -> "DriftConfig":
        if self.drift_mode not in {"abrupt", "gradual"}:
            raise ValueError("drift_mode must be abrupt or gradual")
        if self.scenario != "user_purchase_frequency":
            raise ValueError("only user_purchase_frequency drift is supported")
        if self.enabled:
            if (
                self.drift_start_date is None
                or self.baseline_start_date is None
                or self.baseline_end_date is None
            ):
                raise ValueError("enabled drift requires drift and baseline dates")
            if self.baseline_start_date > self.baseline_end_date:
                raise ValueError("baseline_start_date must be <= baseline_end_date")
            if self.baseline_end_date >= self.drift_start_date:
                raise ValueError("baseline must end before drift_start_date")
            if self.purchase_probability_multiplier <= 1:
                raise ValueError("enabled drift multiplier must be > 1")
        return self


class OutputConfig(BaseModel):
    base_path: str = "apps/data-platform/data-generator/src/output"
    run_id: str
    overwrite: bool = True


class GeneratorConfig(BaseModel):
    seed: int
    history_start_date: date
    history_days: int = Field(gt=0)
    entities: EntityConfig
    traffic: TrafficConfig
    session_behavior: SessionBehaviorConfig
    distribution: DistributionConfig
    challenges: ChallengeConfig
    burst_windows: list[BurstWindow] = Field(default_factory=list)
    schema_evolution: SchemaEvolutionConfig
    drift: DriftConfig = Field(default_factory=DriftConfig)
    output: OutputConfig

    @model_validator(mode="after")
    def validate_dates(self) -> "GeneratorConfig":
        history_end = self.history_start_date.toordinal() + self.history_days
        change_day = self.schema_evolution.change_date.toordinal()
        if not self.history_start_date.toordinal() < change_day < history_end:
            raise ValueError("schema change date must fall inside the history window")
        breaking_change_date = self.schema_evolution.breaking_change_date
        if breaking_change_date is not None:
            breaking_day = breaking_change_date.toordinal()
            if not self.history_start_date.toordinal() < breaking_day < history_end:
                raise ValueError(
                    "breaking schema change date must fall inside the history window"
                )
        if self.entities.n_categories > self.entities.n_products:
            raise ValueError("n_categories cannot exceed n_products")
        if self.entities.n_brands > self.entities.n_products:
            raise ValueError("n_brands cannot exceed n_products")
        if self.drift.enabled:
            history_start = self.history_start_date
            history_end_date = date.fromordinal(history_end - 1)
            drift_dates = (
                self.drift.baseline_start_date,
                self.drift.baseline_end_date,
                self.drift.drift_start_date,
            )
            if any(
                value is None
                or value < history_start
                or value > history_end_date
                for value in drift_dates
            ):
                raise ValueError("drift and baseline dates must be inside history")
        return self


def load_config(path: str | Path) -> GeneratorConfig:
    config_path = Path(path)
    with config_path.open("r", encoding="utf-8") as file:
        payload = yaml.safe_load(file)
    return GeneratorConfig.model_validate(payload)
