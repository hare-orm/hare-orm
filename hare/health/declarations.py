"""What a health check reads and reports - declarations only."""

from __future__ import annotations

import dataclasses
from collections.abc import Sequence
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.client.declarations import PingResult
    from hare.health.criteria.health_criterion import HealthCriterion
    from hare.health.enums import HealthStatus
    from hare.instrumentation.declarations import PoolStatus


@dataclasses.dataclass(frozen=True, slots=True)
class ConnectionCriteria:
    """The criteria of one connection, over the health check's own.

    Attributes:
        degraded_when: The criteria making the connection degraded - the health check's when None.
        unhealthy_when: The criteria making the connection unhealthy - the health check's when None.
    """

    degraded_when: Sequence[HealthCriterion] | None = None
    unhealthy_when: Sequence[HealthCriterion] | None = None


@dataclasses.dataclass(frozen=True, slots=True)
class PoolChange:
    """One pool of a connection now, and what it did since the health check's last run.

    Attributes:
        status: The pool now.
        taken: The connections taken since the last check.
        timeouts: The waits for a connection that ran out since the last check.
        connect_failures: The failures to open a connection since the last check.
        waits: The waits for a connection measured since the last check (``PoolMetrics``), in seconds.
    """

    status: PoolStatus
    taken: int
    timeouts: int
    connect_failures: int
    waits: tuple[float, ...]


@dataclasses.dataclass(frozen=True, slots=True)
class ConnectionCheck:
    """What a criterion judges a connection by.

    Attributes:
        connection_alias: The connection.
        ping: The ping of the connection - None when the check ran without pinging.
        server_ping: The ping of the server past the connection's pooler - None when the check didn't
            ask for it (``check_direct``) or the connection has no pooler.
        pools: The pools of the connection open now - none for a client reporting no pool status.
    """

    connection_alias: str
    ping: PingResult | None
    server_ping: PingResult | None
    pools: tuple[PoolChange, ...]


@dataclasses.dataclass(frozen=True, slots=True)
class ConnectionHealth:
    """How usable one connection is.

    Attributes:
        status: The status.
        latency_ms: How long its ping took - None without a ping or when it failed.
        reasons: Why the status is what it is - a reason of each criterion that held.
        error_type: The name of the exception's class the ping failed with - never its text, which
            may hold the server's address.
        pools: The pools of the connection open now.
    """

    status: HealthStatus
    latency_ms: float | None
    reasons: tuple[str, ...]
    error_type: str | None
    pools: tuple[PoolStatus, ...]
