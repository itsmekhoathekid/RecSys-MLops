from __future__ import annotations

import copy
import random
from datetime import datetime

from streaming.types import EventBundle


class DuplicateReplayProblem:
    def __init__(self, rng: random.Random, rate: float, history_size: int):
        self.rng = rng
        self.rate = rate
        self.history_size = history_size
        self.history: list[EventBundle] = []

    def remember(self, rows: EventBundle) -> None:
        self.history.append(copy.deepcopy(rows))
        self.history = self.history[-self.history_size :]

    def replay(self, now: datetime) -> EventBundle | None:
        if not self.history or self.rng.random() >= self.rate:
            return None
        rows = copy.deepcopy(self.rng.choice(self.history))
        behavior = rows["behavior_events"]
        behavior["created_ts"] = now
        behavior["ingestion_ts"] = now
        for table in ("sessions", "recommendation_requests", "impressions", "orders"):
            if table not in rows:
                continue
            if "updated_ts" in rows[table]:
                rows[table]["updated_ts"] = now
            if "created_ts" in rows[table]:
                rows[table]["created_ts"] = now
        return rows
