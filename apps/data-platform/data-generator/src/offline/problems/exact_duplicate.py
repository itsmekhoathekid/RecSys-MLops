from __future__ import annotations

import numpy as np

from domain import BehaviorEvent


class ExactDuplicateProblem:
    def __init__(self, rng: np.random.Generator, rate: float):
        self.rng = rng
        self.rate = rate

    def apply(self, events: list[BehaviorEvent]) -> tuple[list[BehaviorEvent], int]:
        duplicates = [event for event in events if self.rng.random() < self.rate]
        return duplicates, len(duplicates)
