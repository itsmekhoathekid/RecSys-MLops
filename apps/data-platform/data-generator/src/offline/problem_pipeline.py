from __future__ import annotations

import numpy as np

from config import ChallengeConfig
from domain import BehaviorEvent
from offline.problems import (
    ChallengeStats,
    ExactDuplicateProblem,
    PayloadHashProblem,
    SchemaEvolutionProblem,
)


class OfflineProblemPipeline:
    """Apply historical problems in one explicit, deterministic order."""

    def __init__(
        self,
        rng: np.random.Generator,
        config: ChallengeConfig,
        schema_change_date,
        breaking_schema_change_date=None,
        breaking_schema_version: int = 3,
    ):
        self.schema_evolution = SchemaEvolutionProblem(
            schema_change_date, breaking_schema_change_date, breaking_schema_version
        )
        self.payload_hash = PayloadHashProblem()
        self.exact_duplicate = ExactDuplicateProblem(rng, config.duplicate_event_rate)

    def apply(
        self, clean_events: list[BehaviorEvent]
    ) -> tuple[list[BehaviorEvent], ChallengeStats]:
        normalized: list[BehaviorEvent] = []
        counts = {"v1": 0, "v2": 0, "v3": 0}

        for event in clean_events:
            event, version = self.schema_evolution.apply(event)
            counts[f"v{version}"] += 1
            normalized.append(self.payload_hash.apply(event))

        exact_duplicates, exact_count = self.exact_duplicate.apply(normalized)
        output = [*normalized, *exact_duplicates]
        output.sort(key=lambda event: (event.ingestion_ts, str(event.event_id)))

        return output, ChallengeStats(
            clean_event_count=len(clean_events),
            exact_duplicates_injected=exact_count,
            schema_v1_events=counts["v1"],
            schema_v2_events=counts["v2"],
            schema_v3_events=counts["v3"],
        )


ChallengePipeline = OfflineProblemPipeline
