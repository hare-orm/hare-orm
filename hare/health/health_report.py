from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, Any

from hare.health.enums import HealthStatus

if TYPE_CHECKING:  # pragma: nocoverage
    from collections.abc import Mapping
    from datetime import datetime

    from hare.health.declarations import ConnectionHealth


class HealthReport:
    """How usable every checked connection is, at one moment.

    Args:
        checked_at: When the check ran (UTC).
        connections: How usable each connection is, by name.
    """

    __slots__ = ("checked_at", "connections")

    def __init__(self, checked_at: datetime, connections: Mapping[str, ConnectionHealth]) -> None:
        self.checked_at = checked_at
        self.connections = dict(connections)

    @property
    def status(self) -> HealthStatus:
        """The worst status of the connections - healthy for no connection."""
        return max(
            (connection.status for connection in self.connections.values()),
            # HealthStatus lists the statuses from best to worst.
            key=tuple(HealthStatus).index,
            default=HealthStatus.HEALTHY,
        )

    def to_dict(self, *, include_details: bool = False) -> dict[str, Any]:
        """The report as plain JSON values.

        Args:
            include_details: Whether each connection carries its ping's latency, the reasons of its
                status, its error's type and its pools - else only its status, for an answer anyone
                may read.

        Returns:
            The report.
        """
        connections: dict[str, Any] = {}
        for name, connection in self.connections.items():
            if not include_details:
                connections[name] = {"status": connection.status.value}
                continue
            connections[name] = {
                "status": connection.status.value,
                "latency_ms": connection.latency_ms,
                "reasons": list(connection.reasons),
                "error_type": connection.error_type,
                "pools": [{**dataclasses.asdict(pool), "role": pool.role.value} for pool in connection.pools],
            }
        return {"status": self.status.value, "checked_at": self.checked_at.isoformat(), "connections": connections}

    def __repr__(self) -> str:
        return f"HealthReport(status={self.status.value!r}, connections={sorted(self.connections)!r})"
