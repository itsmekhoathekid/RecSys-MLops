from __future__ import annotations

import asyncio
import json
import logging
import threading

import pytest

from recsys_serving_common import observability
from recsys_serving_common.concurrency import (
    AsyncCapacityLimiter,
    BoundedExecutor,
    CapacityExceeded,
)
from recsys_serving_common.observability import metrics_text, timed_operation


def test_bounded_executor_does_not_release_cancelled_work_early() -> None:
    async def exercise() -> None:
        started = threading.Event()
        release = threading.Event()
        executor = BoundedExecutor(
            workers=1,
            queue_size=0,
            wait_seconds=0.01,
            operation="test_blocking",
        )

        def blocking() -> str:
            started.set()
            release.wait(timeout=1)
            return "finished"

        task = asyncio.create_task(executor.run(blocking))
        await asyncio.to_thread(started.wait, 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        with pytest.raises(CapacityExceeded):
            await executor.run(lambda: "must-not-run")

        release.set()
        for _ in range(100):
            try:
                assert await executor.run(lambda: "accepted") == "accepted"
                break
            except CapacityExceeded:
                await asyncio.sleep(0.001)
        else:
            raise AssertionError("executor capacity was not released on completion")
        await executor.aclose()

    asyncio.run(exercise())


def test_async_capacity_limiter_rejects_when_saturated() -> None:
    async def exercise() -> None:
        limiter = AsyncCapacityLimiter(
            limit=1,
            wait_seconds=0.01,
            operation="test_async",
        )
        entered = asyncio.Event()
        release = asyncio.Event()

        async def hold() -> None:
            async with limiter.slot():
                entered.set()
                await release.wait()

        task = asyncio.create_task(hold())
        await entered.wait()
        with pytest.raises(CapacityExceeded):
            async with limiter.slot():
                raise AssertionError("saturated limiter admitted extra work")
        release.set()
        await task

    asyncio.run(exercise())


def test_bounded_executor_close_waits_for_work_and_rejects_new_submissions() -> None:
    async def exercise() -> None:
        started = threading.Event()
        release = threading.Event()
        executor = BoundedExecutor(
            workers=1,
            queue_size=0,
            wait_seconds=0.01,
            operation="test_close",
        )

        def blocking() -> str:
            started.set()
            release.wait(timeout=1)
            return "finished"

        task = asyncio.create_task(executor.run(blocking))
        await asyncio.to_thread(started.wait, 1)
        close_task = asyncio.create_task(executor.aclose())
        await asyncio.sleep(0)
        assert not close_task.done()

        release.set()
        assert await task == "finished"
        await close_task
        await executor.aclose()
        with pytest.raises(RuntimeError, match="executor is closed"):
            await executor.run(lambda: "must-not-run")

    asyncio.run(exercise())


def test_timed_operation_records_capacity_related_latency() -> None:
    with timed_operation(
        "recsys_api_capacity_wait_seconds",
        labels={"operation": "test_timing", "kind": "executor"},
    ):
        pass

    assert 'operation="test_timing"' in metrics_text()


def test_observability_helpers_cover_disabled_logging_and_trace_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RECSYS_JSON_LOGS", "0")
    observability.configure_logging()
    monkeypatch.setattr(
        observability,
        "current_trace_context",
        lambda: ("trace-id", "span-id"),
    )
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="capacity available",
        args=(),
        exc_info=None,
    )

    payload = json.loads(observability.JsonFormatter().format(record))

    assert payload["trace_id"] == "trace-id"
    assert payload["span_id"] == "span-id"
