from __future__ import annotations

from hare.classes.declared_subclass import DeclaredSubclass
from hare.health.constants import ACQUIRE_TIMEOUTS_REASON, CONNECT_FAILURES_REASON
from hare.health.criteria.pool_count_criterion import PoolCountCriterion

AcquireTimeouts = DeclaredSubclass.make(
    PoolCountCriterion,
    "AcquireTimeouts",
    __package__,
    """Holds when at least ``at_least`` waits for a connection of a pool of the connection ran out
    (``PoolTimeoutError``) since the last check.

    Args:
        at_least: The waits that ran out - at least 1.
    """,
    counted_attribute="timeouts",
    reason=ACQUIRE_TIMEOUTS_REASON,
)

ConnectFailures = DeclaredSubclass.make(
    PoolCountCriterion,
    "ConnectFailures",
    __package__,
    """Holds when opening a connection of a pool of the connection failed at least ``at_least`` times
    since the last check.

    Args:
        at_least: The failures - at least 1.
    """,
    counted_attribute="connect_failures",
    reason=CONNECT_FAILURES_REASON,
)
