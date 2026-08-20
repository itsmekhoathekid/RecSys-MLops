from __future__ import annotations

import asyncio
import threading

import pytest

from recsys_serving_common.concurrency import (
    AsyncCapacityLimiter,
    BoundedExecutor,
    CapacityExceeded,
)


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
