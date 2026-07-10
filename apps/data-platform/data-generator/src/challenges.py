from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from datetime import timedelta
from decimal import Decimal

import numpy as np

from config import ChallengeConfig
from domain import BehaviorEvent


def event_payload_hash(event: BehaviorEvent) -> str:
    payload = {
        "event_id": str(event.event_id),
        "user_id": event.user_id,
        "session_id": str(event.session_id),
        "request_id": str(event.request_id) if event.request_id else None,
        "impression_id": str(event.impression_id) if event.impression_id else None,
        "event_type": event.event_type,
        "product_id": event.product_id,
        "category_id": event.category_id,
        "brand_id": event.brand_id,
        "price": str(event.price),
        "price_bucket": event.price_bucket,
        "quantity": event.quantity,
        "device_type": event.device_type,
        "source": event.source,
        "campaign_id": event.campaign_id,
        "page_context": event.page_context,
        "rank_position": event.rank_position,
        "order_id": str(event.order_id) if event.order_id else None,
        "schema_version": event.schema_version,
        "drift_enabled": event.drift_enabled,
        "drift_scenario": event.drift_scenario,
        "drift_phase": event.drift_phase,
        "drift_factor": event.drift_factor,
    }
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ChallengeStats:
    clean_event_count: int
    exact_duplicates_injected: int
    conflicting_duplicates_injected: int
    late_arrivals_injected: int
    out_of_order_injected: int
    schema_v1_events: int
    schema_v2_events: int
    schema_v3_events: int


class ChallengePipeline:
    def __init__(
        self,
        rng: np.random.Generator,
        config: ChallengeConfig,
        schema_change_date,
        breaking_schema_change_date=None,
        breaking_schema_version: int = 3,
    ):
        self.rng = rng
        self.config = config
        self.schema_change_date = schema_change_date
        self.breaking_schema_change_date = breaking_schema_change_date
        self.breaking_schema_version = breaking_schema_version

    def apply(
        self, clean_events: list[BehaviorEvent]
    ) -> tuple[list[BehaviorEvent], ChallengeStats]:
        normalized: list[BehaviorEvent] = []
        late_count = 0
        out_of_order_count = 0
        schema_v1_count = 0
        schema_v2_count = 0
        schema_v3_count = 0

        for event in clean_events:
            if (
                self.breaking_schema_change_date is not None
                and event.event_timestamp.date() >= self.breaking_schema_change_date
            ):
                event = replace(event, schema_version=self.breaking_schema_version)
                schema_v3_count += 1
            elif event.event_timestamp.date() < self.schema_change_date:
                event = replace(
                    event,
                    device_type=None,
                    campaign_id=None,
                    schema_version=1,
                )
                schema_v1_count += 1
            else:
                event = replace(event, schema_version=2)
                schema_v2_count += 1

            if self.rng.random() < self.config.late_arrival_rate:
                delay = int(
                    self.rng.integers(
                        self.config.late_delay_minutes_min,
                        self.config.late_delay_minutes_max + 1,
                    )
                )
                event = replace(
                    event,
                    created_ts=event.event_timestamp + timedelta(minutes=delay),
                )
                late_count += 1

            if self.rng.random() < self.config.out_of_order_rate:
                delay_seconds = int(self.rng.integers(60, 30 * 60 + 1))
                event = replace(
                    event,
                    ingestion_ts=max(event.ingestion_ts, event.created_ts)
                    + timedelta(seconds=delay_seconds),
                )
                out_of_order_count += 1
            else:
                event = replace(
                    event,
                    ingestion_ts=max(event.ingestion_ts, event.created_ts)
                    + timedelta(seconds=int(self.rng.integers(1, 21))),
                )

            event = replace(event, payload_hash="")
            event = replace(event, payload_hash=event_payload_hash(event))
            normalized.append(event)

        output = list(normalized)
        conflicting_count = 0
        for event in normalized:
            if self.rng.random() < self.config.conflicting_duplicate_rate:
                changed_price = (event.price * Decimal("1.01")).quantize(
                    Decimal("0.01")
                )
                conflict = replace(event, price=changed_price, payload_hash="")
                conflict = replace(
                    conflict,
                    payload_hash=event_payload_hash(conflict),
                    ingestion_ts=event.ingestion_ts
                    + timedelta(seconds=int(self.rng.integers(1, 121))),
                )
                output.append(conflict)
                conflicting_count += 1

        exact_count = 0
        for event in normalized:
            if self.rng.random() < self.config.duplicate_event_rate:
                output.append(event)
                exact_count += 1

        output.sort(key=lambda event: (event.ingestion_ts, str(event.event_id)))
        return output, ChallengeStats(
            clean_event_count=len(clean_events),
            exact_duplicates_injected=exact_count,
            conflicting_duplicates_injected=conflicting_count,
            late_arrivals_injected=late_count,
            out_of_order_injected=out_of_order_count,
            schema_v1_events=schema_v1_count,
            schema_v2_events=schema_v2_count,
            schema_v3_events=schema_v3_count,
        )
