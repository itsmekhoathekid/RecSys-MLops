from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from uuid import UUID, uuid5

import numpy as np


class DeterministicIds:
    def __init__(self, seed: int):
        self.namespace = UUID("2c21b435-b243-4c06-9f75-f595dc6b02d4")
        self.seed = seed
        self.counters: dict[str, int] = defaultdict(int)

    def next(self, entity: str) -> UUID:
        self.counters[entity] += 1
        return uuid5(self.namespace, f"{self.seed}:{entity}:{self.counters[entity]}")


def utc_datetime(day: date, seconds_after_midnight: int = 0) -> datetime:
    return datetime.combine(day, time.min, tzinfo=timezone.utc) + timedelta(
        seconds=seconds_after_midnight
    )


def weighted_index(rng: np.random.Generator, weights: list[float]) -> int:
    probabilities = np.asarray(weights, dtype=np.float64)
    probabilities /= probabilities.sum()
    return int(rng.choice(len(weights), p=probabilities))
