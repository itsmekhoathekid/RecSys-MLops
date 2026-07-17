from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StreamGeneratorSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    interval_seconds: float = Field(default=1.0, ge=0)
    events_per_tick: int = Field(default=40, gt=0)
    max_events: int = Field(default=0, ge=0)
    n_users: int = Field(default=80, gt=0)
    n_products: int = Field(default=160, gt=0)


class BurstTrafficConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    every_n_ticks: int = Field(default=0, ge=0)
    multiplier: int = Field(default=1, gt=0)


class DuplicateReplayConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rate: float = Field(default=0.0, ge=0, le=1)
    history_size: int = Field(default=1000, gt=0)


class LateArrivalConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rate: float = Field(default=0.0, ge=0, le=1)
    delay_minutes_min: int = Field(default=5, gt=0)
    delay_minutes_max: int = Field(default=45, gt=0)

    @model_validator(mode="after")
    def validate_delay(self) -> "LateArrivalConfig":
        if self.delay_minutes_min > self.delay_minutes_max:
            raise ValueError("late-arrival delay min must be <= max")
        return self


class StreamProblemsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    burst_traffic: BurstTrafficConfig = Field(default_factory=BurstTrafficConfig)
    duplicate_replay: DuplicateReplayConfig = Field(
        default_factory=DuplicateReplayConfig
    )
    late_arrival: LateArrivalConfig = Field(default_factory=LateArrivalConfig)


class StreamSectionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    generator: StreamGeneratorSettings = Field(default_factory=StreamGeneratorSettings)
    problems: StreamProblemsConfig = Field(default_factory=StreamProblemsConfig)


class StreamGeneratorConfig(StreamSectionConfig):
    """Runtime streaming config with the shared document seed attached."""

    seed: int = 42
