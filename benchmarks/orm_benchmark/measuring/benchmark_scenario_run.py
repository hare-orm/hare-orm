from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Iterable
from typing import Any

from orm_benchmark.definitions.measurement import Measurement


class BenchmarkScenarioRun:
    """Apply the same preparation, warm-up and timing boundaries to every ORM.

    The callable performs only the operation being compared. Preparation precedes
    each invocation outside the timer; destructive scenarios request one repetition.
    Errors propagate to the process instead of becoming unsupported observations.
    """

    @staticmethod
    async def await_operation(operation: Awaitable[Any]) -> None:
        """Accept ORM query objects as well as native coroutines in a task group."""
        await operation

    @classmethod
    async def concurrent(cls, operations: Iterable[Awaitable[Any]]) -> None:
        """Join every operation before exposing failure or cancellation to cleanup."""
        async with asyncio.TaskGroup() as workers:
            for operation in operations:
                workers.create_task(cls.await_operation(operation))

    @staticmethod
    async def measure(
        scenario: Callable[[], Awaitable[Any]],
        repetitions: int,
        prepare: Callable[[], Awaitable[Any]] | None = None,
        *,
        warmup: int = 0,
        conditions: dict[str, Any] | None = None,
    ) -> Measurement:
        samples = []
        for index in range(warmup + repetitions):
            if prepare is not None:
                await prepare()
            if index < warmup:
                await scenario()
                continue
            start = time.perf_counter()
            await scenario()
            samples.append((time.perf_counter() - start) * 1000)
        return Measurement(tuple(samples), warmup_operations=warmup, conditions=conditions or {})
