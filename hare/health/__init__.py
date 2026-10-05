"""How usable the connections are: ``HealthCheck`` pings them and judges them by criteria
(``hare.health.criteria``) into a ``HealthReport``."""

from __future__ import annotations

from hare.health.declarations import ConnectionCheck, ConnectionCriteria, ConnectionHealth, PoolChange
from hare.health.enums import HealthStatus
from hare.health.health_check import HealthCheck
from hare.health.health_report import HealthReport

__all__ = [
    "ConnectionCheck",
    "ConnectionCriteria",
    "ConnectionHealth",
    "HealthCheck",
    "HealthReport",
    "HealthStatus",
    "PoolChange",
]
