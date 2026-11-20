from __future__ import annotations

from dataclasses import dataclass

from hare.instrumentation.pools.enums import PoolMetricEvent


@dataclass(frozen=True, slots=True)
class PoolMeasurement:
    """Completed event with labels copied before dispatch; no connection context is required."""

    connection_alias: str
    role: str
    schema: str | None
    system: str
    event: PoolMetricEvent
    value: float
