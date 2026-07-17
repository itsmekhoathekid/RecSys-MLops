import random
from datetime import datetime, timedelta


class LateArrivalProblem:
    def __init__(
        self, rng: random.Random, rate: float, delay_min: int, delay_max: int
    ):
        self.rng = rng
        self.rate = rate
        self.delay_min = delay_min
        self.delay_max = delay_max

    def apply(self, now: datetime) -> tuple[datetime, int] | None:
        if self.rng.random() >= self.rate:
            return None
        delay_minutes = self.rng.randint(self.delay_min, self.delay_max)
        return now - timedelta(minutes=delay_minutes), delay_minutes * 60
