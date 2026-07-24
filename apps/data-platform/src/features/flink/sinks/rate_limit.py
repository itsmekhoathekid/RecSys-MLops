from __future__ import annotations

import asyncio
import time
from collections.abc import Callable


class TokenBucketRateLimiter:
    """Bound one Flink sink subtask without buffering records in process memory."""

    def __init__(
        self,
        max_events_per_second: float,
        burst_events: int = 1,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if burst_events <= 0:
            raise ValueError("burst_events must be positive")
        self.rate = max(0.0, float(max_events_per_second))
        self.capacity = float(burst_events)
        self.clock = clock
        self.sleeper = sleeper
        self.tokens = self.capacity
        self.updated_at = self.clock()

    @property
    def enabled(self) -> bool:
        return self.rate > 0

    def acquire(self) -> float:
        """Consume one event token and return seconds spent applying backpressure."""
        if not self.enabled:
            return 0.0

        now = self.clock()
        self.tokens = min(
            self.capacity, self.tokens + max(0.0, now - self.updated_at) * self.rate
        )
        self.updated_at = now
        if self.tokens >= 1.0:
            self.tokens -= 1.0
            return 0.0

        wait_seconds = (1.0 - self.tokens) / self.rate
        self.sleeper(wait_seconds)
        resumed_at = self.clock()
        self.tokens = min(
            self.capacity,
            self.tokens + max(0.0, resumed_at - self.updated_at) * self.rate,
        )
        self.updated_at = resumed_at
        self.tokens = max(0.0, self.tokens - 1.0)
        return wait_seconds


class AsyncTokenBucketRateLimiter:
    """Async token bucket for bounded Flink AsyncDataStream requests."""

    def __init__(
        self,
        max_events_per_second: float,
        burst_events: int = 1,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if burst_events <= 0:
            raise ValueError("burst_events must be positive")
        self.rate = max(0.0, float(max_events_per_second))
        self.capacity = float(burst_events)
        self.clock = clock
        self.tokens = self.capacity
        self.updated_at = self.clock()
        self._lock: asyncio.Lock | None = None

    @property
    def enabled(self) -> bool:
        return self.rate > 0

    async def acquire(self) -> float:
        """Consume one token without blocking the async worker event loop."""
        if not self.enabled:
            return 0.0
        if self._lock is None:
            self._lock = asyncio.Lock()

        async with self._lock:
            now = self.clock()
            self.tokens = min(
                self.capacity,
                self.tokens + max(0.0, now - self.updated_at) * self.rate,
            )
            self.updated_at = now
            if self.tokens >= 1.0:
                self.tokens -= 1.0
                return 0.0

            wait_seconds = (1.0 - self.tokens) / self.rate
            await asyncio.sleep(wait_seconds)
            resumed_at = self.clock()
            self.tokens = min(
                self.capacity,
                self.tokens + max(0.0, resumed_at - self.updated_at) * self.rate,
            )
            self.updated_at = resumed_at
            self.tokens = max(0.0, self.tokens - 1.0)
            return wait_seconds
