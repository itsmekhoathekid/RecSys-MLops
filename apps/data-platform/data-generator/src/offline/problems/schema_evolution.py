from __future__ import annotations

from dataclasses import replace
from datetime import date

from domain import BehaviorEvent


class SchemaEvolutionProblem:
    def __init__(
        self,
        change_date: date,
        breaking_change_date: date | None = None,
        breaking_schema_version: int = 3,
    ):
        self.change_date = change_date
        self.breaking_change_date = breaking_change_date
        self.breaking_schema_version = breaking_schema_version

    def apply(self, event: BehaviorEvent) -> tuple[BehaviorEvent, int]:
        event_date = event.event_timestamp.date()
        if (
            self.breaking_change_date is not None
            and event_date >= self.breaking_change_date
        ):
            return replace(event, schema_version=self.breaking_schema_version), 3
        if event_date < self.change_date:
            return replace(event, device_type=None, campaign_id=None, schema_version=1), 1
        return replace(event, schema_version=2), 2
