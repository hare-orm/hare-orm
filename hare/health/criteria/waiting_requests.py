from __future__ import annotations

from typing import TYPE_CHECKING

from hare.health.constants import WAITING_REQUESTS_REASON
from hare.health.criteria.health_criterion import HealthCriterion

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.health.declarations import ConnectionCheck


class WaitingRequests(HealthCriterion):
    """Holds when at least ``at_least`` tasks wait for a connection of a pool of the connection.

    Args:
        at_least: The waiting tasks - at least 1.
    """

    def __init__(self, at_least: int) -> None:
        self.at_least = HealthCriterion.get_checked_count("at_least", at_least)

    def check(self, connection_check: ConnectionCheck) -> str | None:
        for pool in connection_check.pools:
            if pool.status.waiting >= self.at_least:
                return WAITING_REQUESTS_REASON.format(
                    waiting=pool.status.waiting,
                    pool=HealthCriterion.describe_pool(pool.status),
                    at_least=self.at_least,
                )
        return None

    def __repr__(self) -> str:
        return f"WaitingRequests(at_least={self.at_least})"
