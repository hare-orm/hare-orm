from __future__ import annotations

import asyncio
import itertools
import time
from collections.abc import Callable, Coroutine
from typing import Any

from orm_benchmark.constants import LOAD_OPERATION_CYCLE, LOAD_WARM_UP_ROUNDS
from orm_benchmark.definitions.measurement import Measurement


class LoadTest:
    """Sustained concurrent load: workers pull operations off a shared counter until all are done, all
    racing for the same connection pool."""

    @staticmethod
    async def run(
        name: str, operation: Callable[[int], Coroutine[Any, Any, object]], concurrency: int, total: int
    ) -> dict[str, Measurement]:
        """Runs a load test after an untimed warm-up: the state of an application running for a
        while, not of one just started.

        Args:
            name: The prefix of its figures in the results - ``load_test``, ``write_load_test``.
            operation: Runs operation number ``index`` - its type follows ``index % 20``.
            concurrency: How many workers run at once.
            total: How many operations the timed round runs.

        Returns:
            Wall-clock duration, throughput and actual per-operation latency samples.
            Latency includes the operation's own pool wait, not time before a worker
            claims it. Task groups drain all workers before failure or cancellation
            reaches the caller, so database cleanup cannot race a surviving worker.
        """
        counter = itertools.count()
        latencies = [0.0] * total

        async def worker() -> None:
            while True:
                index = next(counter)
                if index >= total:
                    return
                start = time.perf_counter()
                await operation(index)
                latencies[index] = (time.perf_counter() - start) * 1000

        # Exercise every operation concurrently before timing. Pool scheduling decides which
        # connections serve these workers; this is not a per-connection preparation guarantee.
        for __ in range(LOAD_WARM_UP_ROUNDS):
            for offset in range(LOAD_OPERATION_CYCLE):
                async with asyncio.TaskGroup() as workers:
                    for number in range(concurrency):
                        workers.create_task(operation(offset + LOAD_OPERATION_CYCLE * number))
        start = time.perf_counter()
        async with asyncio.TaskGroup() as workers:
            for _ in range(concurrency):
                workers.create_task(worker())
        elapsed_seconds = time.perf_counter() - start
        warmup = LOAD_WARM_UP_ROUNDS * LOAD_OPERATION_CYCLE * concurrency
        conditions = {"concurrency": concurrency, "operations": total, "operation_cycle": LOAD_OPERATION_CYCLE}
        return {
            f"{name}_total_ms": Measurement(
                (elapsed_seconds * 1000,), warmup_operations=warmup, conditions=conditions
            ),
            f"{name}_throughput_ops_per_sec": Measurement(
                (total / elapsed_seconds,), unit="ops/s", warmup_operations=warmup, conditions=conditions
            ),
            f"{name}_avg_latency_ms": Measurement(
                tuple(latencies), statistic="mean", warmup_operations=warmup, conditions=conditions
            ),
        }
