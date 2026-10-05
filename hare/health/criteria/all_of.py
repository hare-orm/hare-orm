from __future__ import annotations

from typing import TYPE_CHECKING

from hare.health.criteria.health_criterion import HealthCriterion

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.health.declarations import ConnectionCheck


class AllOf(HealthCriterion):
    """Holds when every one of its criteria holds - ``AllOf(PoolSaturation(ratio=1.0),
    WaitingRequests(at_least=3))``.

    Args:
        *criteria: The criteria - at least two.

    Raises:
        TypeError: A criterion isn't a ``HealthCriterion``.
        ValueError: Fewer than two criteria.
    """

    def __init__(self, *criteria: HealthCriterion) -> None:
        if len(criteria) < 2:
            raise ValueError(f"AllOf needs at least two criteria, got {len(criteria)}")
        for criterion in criteria:
            if not isinstance(criterion, HealthCriterion):
                raise TypeError(f"AllOf takes HealthCriterion objects, got {type(criterion).__name__}")
        self.criteria = criteria
        self.requires_pool_metrics = any(criterion.requires_pool_metrics for criterion in criteria)

    def check(self, connection_check: ConnectionCheck) -> str | None:
        reasons = []
        for criterion in self.criteria:
            reason = criterion.check(connection_check)
            if reason is None:
                return None
            reasons.append(reason)
        return "; ".join(reasons)

    def __repr__(self) -> str:
        return f"AllOf({', '.join(repr(criterion) for criterion in self.criteria)})"
