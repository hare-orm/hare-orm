from __future__ import annotations

from enum import StrEnum


class HealthStatus(StrEnum):
    """How usable a connection is - in the order from best to worst."""

    #: The connection answers and no degrading criterion holds.
    HEALTHY = "healthy"
    #: The connection answers, but a degrading criterion holds - the pool is busy, waits are long.
    DEGRADED = "degraded"
    #: An unhealthy criterion holds - by default the ping failed.
    UNHEALTHY = "unhealthy"
