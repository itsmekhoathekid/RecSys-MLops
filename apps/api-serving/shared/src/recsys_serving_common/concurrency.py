"""Bounded async concurrency adapters for serving workloads."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from functools import partial
from typing import Any, TypeVar

from recsys_serving_common.observability import METRICS


T = TypeVar("T")


class CapacityExceeded(RuntimeError):
    """Raised when an operation cannot acquire bounded capacity in time."""


class AsyncCapacityLimiter:
    """Bound concurrent async operations and expose wait/saturation metrics."""

    def __init__(
        self,
        *,
        limit: int,
        wait_seconds: float,
        operation: str,
    ) -> None:
        self.limit = max(1, int(limit))
        self.wait_seconds = max(0.001, float(wait_seconds))
        self.operation = operation
        self._semaphore = asyncio.Semaphore(self.limit)
        self._in_flight = 0

    @asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        start = time.perf_counter()
        try:
            await asyncio.wait_for(self._semaphore.acquire(), timeout=self.wait_seconds)
        except (asyncio.TimeoutError, TimeoutError) as exc:
            METRICS.inc(
                "recsys_api_capacity_rejections_total",
                labels={"operation": self.operation, "kind": "async"},
            )
            raise CapacityExceeded(f"{self.operation} capacity exhausted") from exc
        METRICS.observe(
            "recsys_api_capacity_wait_seconds",
            time.perf_counter() - start,
            labels={"operation": self.operation, "kind": "async"},
        )
        self._in_flight += 1
        METRICS.set_gauge(
            "recsys_api_in_flight_operations",
            self._in_flight,
            labels={"operation": self.operation, "kind": "async"},
        )
        try:
            yield
        finally:
            self._in_flight = max(0, self._in_flight - 1)
            METRICS.set_gauge(
                "recsys_api_in_flight_operations",
                self._in_flight,
                labels={"operation": self.operation, "kind": "async"},
            )
            self._semaphore.release()


class BoundedExecutor:
    """Run blocking calls without allowing an unbounded executor queue.

    Capacity is released by the underlying executor future, not by the awaiting
    request. A cancelled HTTP request therefore cannot make a still-running
    blocking operation disappear from concurrency accounting.
    """

    def __init__(
        self,
        *,
        workers: int,
        queue_size: int,
        wait_seconds: float,
        operation: str,
    ) -> None:
        self.workers = max(1, int(workers))
        self.queue_size = max(0, int(queue_size))
        self.wait_seconds = max(0.001, float(wait_seconds))
        self.operation = operation
        self._executor = ThreadPoolExecutor(
            max_workers=self.workers,
            thread_name_prefix=f"recsys-{operation}",
        )
        self._capacity = asyncio.Semaphore(self.workers + self.queue_size)
        self._futures: set[asyncio.Future[Any]] = set()
        self._in_flight = 0
        self._closed = False

    async def run(self, function: Callable[..., T], /, *args: Any, **kwargs: Any) -> T:
        if self._closed:
            raise RuntimeError(f"{self.operation} executor is closed")
        start = time.perf_counter()
        try:
            await asyncio.wait_for(self._capacity.acquire(), timeout=self.wait_seconds)
        except (asyncio.TimeoutError, TimeoutError) as exc:
            METRICS.inc(
                "recsys_api_capacity_rejections_total",
                labels={"operation": self.operation, "kind": "executor"},
            )
            raise CapacityExceeded(f"{self.operation} capacity exhausted") from exc
        METRICS.observe(
            "recsys_api_capacity_wait_seconds",
            time.perf_counter() - start,
            labels={"operation": self.operation, "kind": "executor"},
        )
        loop = asyncio.get_running_loop()
        try:
            future = loop.run_in_executor(
                self._executor, partial(function, *args, **kwargs)
            )
        except BaseException:
            self._capacity.release()
            raise
        self._futures.add(future)
        self._in_flight += 1
        METRICS.set_gauge(
            "recsys_api_in_flight_operations",
            self._in_flight,
            labels={"operation": self.operation, "kind": "executor"},
        )
        future.add_done_callback(self._complete)
        return await asyncio.shield(future)

    def _complete(self, future: asyncio.Future[Any]) -> None:
        self._futures.discard(future)
        self._in_flight = max(0, self._in_flight - 1)
        METRICS.set_gauge(
            "recsys_api_in_flight_operations",
            self._in_flight,
            labels={"operation": self.operation, "kind": "executor"},
        )
        self._capacity.release()

    async def aclose(self) -> None:
        """Stop submissions and wait for accepted calls before shutdown."""

        if self._closed:
            return
        self._closed = True
        if self._futures:
            await asyncio.gather(
                *(asyncio.shield(future) for future in tuple(self._futures)),
                return_exceptions=True,
            )
        await asyncio.to_thread(self._executor.shutdown, wait=True, cancel_futures=True)
