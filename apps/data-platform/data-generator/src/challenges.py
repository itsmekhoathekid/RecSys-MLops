"""Backward-compatible imports for the historical problem injector."""

from offline.problem_pipeline import ChallengePipeline, OfflineProblemPipeline
from offline.problems import ChallengeStats, event_payload_hash

__all__ = [
    "ChallengePipeline",
    "ChallengeStats",
    "OfflineProblemPipeline",
    "event_payload_hash",
]
