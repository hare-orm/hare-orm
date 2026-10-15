from __future__ import annotations

from typing import TYPE_CHECKING

from hare.health.constants import PING_FAILED_REASON, SERVER_PING_FAILED_REASON
from hare.health.criteria.health_criterion import HealthCriterion

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.health.declarations import ConnectionCheck


class PingFailed(HealthCriterion):
    """Holds when the connection's ping - or the ping of the server past its pooler
    (``check_direct``) - failed or didn't answer within the health check's timeout. Never holds for a
    check run without pinging."""

    def check(self, connection_check: ConnectionCheck) -> str | None:
        ping = connection_check.ping
        if ping is not None and not ping.succeeded:
            return PING_FAILED_REASON.format(error_type=ping.error_type)
        server_ping = connection_check.server_ping
        if server_ping is not None and not server_ping.succeeded:
            return SERVER_PING_FAILED_REASON.format(error_type=server_ping.error_type)
        return None

    def __repr__(self) -> str:
        return "PingFailed()"
