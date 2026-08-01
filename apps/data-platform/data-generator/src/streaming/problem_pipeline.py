from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime

from streaming.config import StreamProblemsConfig
from streaming.problems import (
    BurstTrafficProblem,
    DuplicateReplayProblem,
    LateArrivalProblem,
)
from streaming.types import EventBundle


@dataclass(frozen=True)
class StreamProblemResult:
    event_timestamp: datetime
    late: bool = False
    delay_seconds: int = 0


class StreamProblemPipeline:
    def __init__(self, rng: random.Random, config: StreamProblemsConfig):
        self.burst = BurstTrafficProblem(
            config.burst_traffic.every_n_ticks, config.burst_traffic.multiplier
        )
        self.duplicates = DuplicateReplayProblem(
            rng, config.duplicate_replay.rate, config.duplicate_replay.history_size
        )
        self.late_arrival = LateArrivalProblem(
            rng,
            config.late_arrival.rate,
            config.late_arrival.delay_minutes_min,
            config.late_arrival.delay_minutes_max,
        )

    def event_time(self, now: datetime) -> StreamProblemResult:
        late = self.late_arrival.apply(now)
        if late is not None:
            return StreamProblemResult(late[0], late=True, delay_seconds=late[1])
        return StreamProblemResult(now)

    def replay(self, now: datetime) -> EventBundle | None:
        return self.duplicates.replay(now)
