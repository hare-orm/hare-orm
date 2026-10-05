from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from hare.health.criteria.health_criterion import HealthCriterion

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.health.declarations import ConnectionCheck


class PoolCountCriterion(HealthCriterion):
    """Holds when a count a pool of the connection keeps since the last check reached ``at_least`` -
    a subclass names the count and the reason it gives.

    Args:
        at_least: The count - at least 1.
    """

    #: The attribute of a pool's check holding the count.
    counted_attribute: ClassVar[str]
    #: The reason given - formatted with ``count``, ``pool`` and ``at_least``.
    reason: ClassVar[str]

    def __init__(self, at_least: int) -> None:
        self.at_least = HealthCriterion.get_checked_count("at_least", at_least)

    def check(self, connection_check: ConnectionCheck) -> str | None:
        for pool in connection_check.pools:
            count = getattr(pool, self.counted_attribute)
            if count >= self.at_least:
                return self.reason.format(
                    count=count, pool=HealthCriterion.describe_pool(pool.status), at_least=self.at_least
                )
        return None

    def __repr__(self) -> str:
        return f"{type(self).__name__}(at_least={self.at_least})"
