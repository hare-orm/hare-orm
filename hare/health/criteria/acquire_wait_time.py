from __future__ import annotations

from typing import TYPE_CHECKING

from hare.health.constants import ACQUIRE_WAIT_TIME_REASON, MAX_ACQUIRE_WAIT_SECONDS
from hare.health.criteria.health_criterion import HealthCriterion

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.health.declarations import ConnectionCheck


class AcquireWaitTime(HealthCriterion):
    """Holds when a wait for a connection of a pool of the connection took longer than ``max_seconds``
    since the last check. Reads the measured waits - ``PoolMetrics`` must be enabled.

    Args:
        max_seconds: The longest wait that passes - above 0, at most 3600.
    """

    requires_pool_metrics: bool = True

    def __init__(self, max_seconds: float) -> None:
        self.max_seconds = HealthCriterion.get_checked_amount("max_seconds", max_seconds, MAX_ACQUIRE_WAIT_SECONDS)

    def check(self, connection_check: ConnectionCheck) -> str | None:
        for pool in connection_check.pools:
            longest_wait = max(pool.waits, default=0.0)
            if longest_wait > self.max_seconds:
                return ACQUIRE_WAIT_TIME_REASON.format(
                    pool=HealthCriterion.describe_pool(pool.status), seconds=longest_wait, max_seconds=self.max_seconds
                )
        return None

    def __repr__(self) -> str:
        return f"AcquireWaitTime(max_seconds={self.max_seconds:g})"
