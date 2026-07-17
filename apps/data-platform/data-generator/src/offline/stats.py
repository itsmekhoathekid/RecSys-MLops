from dataclasses import dataclass


@dataclass(frozen=True)
class ChallengeStats:
    clean_event_count: int
    exact_duplicates_injected: int
    schema_v1_events: int
    schema_v2_events: int
    schema_v3_events: int
