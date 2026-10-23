from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable

from orm_benchmark.constants import LOAD_OPERATION_CYCLE, LOAD_WARM_UP_ROUNDS


class LoadTest:
    """Sustained concurrent load: workers pull operations off a shared counter until all are done, all
    racing for the same connection pool."""

    @staticmethod
    async def run(
        name: str, operation: Callable[[int], Awaitable[object]], concurrency: int, total: int
    ) -> dict[str, float]:
        """Runs a load test after an untimed warm-up: the state of an application running for a
        while, not of one just started.

        Args:
            name: The prefix of its figures in the results - ``load_test``, ``write_load_test``.
            operation: Runs operation number ``index`` - its type follows ``index % 20``.
            concurrency: How many workers run at once.
            total: How many operations the timed round runs.

        Returns:
            The wall-clock time, the throughput and the average latency under the load.
        """
        lock = asyncio.Lock()

        async def worker(counter: dict[str, int], operations: int) -> None:
            while True:
                async with lock:
                    if counter["done"] >= operations:
                        return
                    index = counter["done"]
                    counter["done"] += 1
                await operation(index)

        # Every pool connection runs every operation of the load before the timed round: each
        # operation of the cycle runs on every worker at once, so each takes a connection of its
        # own. Opening a connection and preparing a statement on it are one-time costs of an
        # application's start, not the ORM's per-operation overhead.
        for __ in range(LOAD_WARM_UP_ROUNDS):
            for offset in range(LOAD_OPERATION_CYCLE):
                await asyncio.gather(
                    *[operation(offset + LOAD_OPERATION_CYCLE * number) for number in range(concurrency)]
                )
        counter = {"done": 0}
        start = time.perf_counter()
        await asyncio.gather(*[worker(counter, total) for __ in range(concurrency)])
        elapsed_seconds = time.perf_counter() - start
        return {
            f"{name}_total_ms": elapsed_seconds * 1000,
            f"{name}_throughput_ops_per_sec": total / elapsed_seconds,
            f"{name}_avg_latency_ms": elapsed_seconds * 1000 / total * concurrency,
        }
