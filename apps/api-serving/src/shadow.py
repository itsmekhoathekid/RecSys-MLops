from __future__ import annotations

import asyncio
import time

import numpy as np

from ab_testing import TritonRoute
from observability import LATENCY_BUCKETS, METRICS
from serving_utils import ab_labels


class ShadowRunner:
    """Best-effort, bounded candidate inference that never blocks the user response."""

    def __init__(
        self,
        *,
        timeout_seconds: float = 1.0,
        max_pending: int = 100,
        max_concurrency: int = 4,
    ) -> None:
        self.timeout_seconds = max(0.001, float(timeout_seconds))
        self.max_pending = max(1, int(max_pending))
        self._semaphore = asyncio.Semaphore(max(1, int(max_concurrency)))
        self._tasks: set[asyncio.Task[None]] = set()

    @property
    def pending_count(self) -> int:
        return len(self._tasks)

    def submit(self, route: TritonRoute | None, payload: dict[str, np.ndarray]) -> bool:
        if route is None:
            return False
        labels = ab_labels("shadow_candidate", route.model_version, route.ab_experiment_id)
        if self.pending_count >= self.max_pending:
            METRICS.inc("recsys_api_shadow_inferences_total", labels={**labels, "status": "dropped"})
            METRICS.set_gauge("recsys_api_shadow_queue_depth", self.pending_count, labels=labels)
            return False
        task = asyncio.create_task(self._run(route, payload, labels))
        self._tasks.add(task)
        METRICS.set_gauge("recsys_api_shadow_queue_depth", self.pending_count, labels=labels)
        task.add_done_callback(self._discard)
        return True

    def _discard(self, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        # _run consumes inference exceptions; result() only surfaces unexpected implementation bugs.
        try:
            task.result()
        except (asyncio.CancelledError, Exception):
            pass

    async def _run(
        self,
        route: TritonRoute,
        payload: dict[str, np.ndarray],
        labels: dict[str, str],
    ) -> None:
        start = time.perf_counter()
        status = "error"
        try:
            async with self._semaphore:
                _, scores = await asyncio.wait_for(
                    asyncio.to_thread(route.ranker.score, payload),
                    timeout=self.timeout_seconds,
                )
            status = "success"
            if scores:
                METRICS.observe("recsys_api_shadow_score_mean", float(np.mean(scores)), labels=labels)
                METRICS.set_gauge("recsys_api_shadow_score_max", float(np.max(scores)), labels=labels)
        except asyncio.TimeoutError:
            status = "timeout"
        except Exception:
            status = "error"
        finally:
            duration = time.perf_counter() - start
            METRICS.inc("recsys_api_shadow_inferences_total", labels={**labels, "status": status})
            METRICS.observe_histogram(
                "recsys_api_shadow_latency_seconds",
                duration,
                labels=labels,
                buckets=LATENCY_BUCKETS,
            )
            METRICS.set_gauge("recsys_api_shadow_queue_depth", max(0, self.pending_count - 1), labels=labels)

    async def drain(self) -> None:
        if self._tasks:
            await asyncio.gather(*tuple(self._tasks), return_exceptions=True)
