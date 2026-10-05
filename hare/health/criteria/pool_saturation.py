from __future__ import annotations

from typing import TYPE_CHECKING

from hare.health.constants import POOL_SATURATION_REASON
from hare.health.criteria.health_criterion import HealthCriterion

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.health.declarations import ConnectionCheck


class PoolSaturation(HealthCriterion):
    """Holds when a pool of the connection has at least ``ratio`` of its most connections in use.

    Args:
        ratio: The share of ``max_size`` - above 0, at most 1.
    """

    def __init__(self, ratio: float) -> None:
        self.ratio = HealthCriterion.get_checked_amount("ratio", ratio, 1.0)

    def check(self, connection_check: ConnectionCheck) -> str | None:
        for pool in connection_check.pools:
            status = pool.status
            if status.max_size and status.in_use >= self.ratio * status.max_size:
                return POOL_SATURATION_REASON.format(
                    in_use=status.in_use,
                    max_size=status.max_size,
                    pool=HealthCriterion.describe_pool(status),
                    ratio=self.ratio,
                )
        return None

    def __repr__(self) -> str:
        return f"PoolSaturation(ratio={self.ratio:g})"
