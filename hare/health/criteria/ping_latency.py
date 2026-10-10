from __future__ import annotations

from typing import TYPE_CHECKING

from hare.health.constants import MAX_PING_LATENCY_MS, PING_LATENCY_REASON
from hare.health.criteria.health_criterion import HealthCriterion

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.health.declarations import ConnectionCheck


class PingLatency(HealthCriterion):
    """Holds when the connection's ping took longer than ``max_ms``.

    Args:
        max_ms: The longest ping that passes, in milliseconds - above 0, at most 60000.
    """

    def __init__(self, max_ms: float) -> None:
        self.max_ms = HealthCriterion.get_checked_amount("max_ms", max_ms, MAX_PING_LATENCY_MS)

    def check(self, connection_check: ConnectionCheck) -> str | None:
        ping = connection_check.ping
        if ping is None or ping.latency_ms is None or ping.latency_ms <= self.max_ms:
            return None
        return PING_LATENCY_REASON.format(latency_ms=ping.latency_ms, max_ms=self.max_ms)

    def __repr__(self) -> str:
        return f"PingLatency(max_ms={self.max_ms:g})"
