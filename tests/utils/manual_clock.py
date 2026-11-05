from __future__ import annotations


class ManualClock:
    """A monotonic clock advanced by explicit work and recorded backoff waits."""

    def __init__(self) -> None:
        self.elapsed = 0.0
        self.delays: list[float] = []

    def perf_counter(self) -> float:
        return self.elapsed

    def advance(self, seconds: float) -> None:
        self.elapsed += seconds

    async def sleep(self, seconds: float) -> None:
        self.delays.append(seconds)
        self.advance(seconds)
