from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Any


class Timing:
    """How a scenario is timed."""

    @staticmethod
    async def get_best(
        scenario: Callable[[], Awaitable[Any]],
        repetitions: int,
        prepare: Callable[[], Awaitable[Any]] | None = None,
    ) -> float:
        """The fastest of ``repetitions`` runs of a scenario, in milliseconds.

        Args:
            scenario: The scenario.
            repetitions: How many times it runs; 1 for a scenario that changes the data for good.
            prepare: Runs before each repetition, untimed - clearing the rows a write puts back.

        Returns:
            The fastest time.
        """
        times = []
        for __ in range(repetitions):
            if prepare is not None:
                await prepare()
            start = time.perf_counter()
            await scenario()
            times.append((time.perf_counter() - start) * 1000)
        return min(times)
