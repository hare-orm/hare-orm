from __future__ import annotations

import asyncio
import weakref
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from hare.core.connections.connections import Connections
from hare.dialects.base.client.connection_ping import ConnectionPing
from hare.dialects.base.client.declarations import PingResult
from hare.exceptions import ConfigurationError
from hare.health.constants import (
    DEFAULT_HEALTH_TIMEOUT_SECONDS,
    DEFAULT_POOL_SATURATION_RATIO,
    MAX_HEALTH_TIMEOUT_SECONDS,
    POOL_METRICS_REQUIRED_MESSAGE,
    UNKNOWN_HEALTH_CONNECTION_MESSAGE,
)
from hare.health.criteria.health_criterion import HealthCriterion
from hare.health.criteria.ping_failed import PingFailed
from hare.health.criteria.pool_saturation import PoolSaturation
from hare.health.criteria.waiting_requests import WaitingRequests
from hare.health.declarations import ConnectionCheck, ConnectionCriteria, ConnectionHealth, PoolChange
from hare.health.enums import HealthStatus
from hare.health.health_report import HealthReport
from hare.instrumentation.pools.pool_metrics import PoolMetrics
from hare.instrumentation.pools.pool_registry import PoolRegistry

if TYPE_CHECKING:
    from hare.core.connections.connection_handler import ConnectionHandler
    from hare.dialects.base.client.database_client import DatabaseClient


class HealthCheck:
    """How usable the connections of the current context are - for a readiness probe, a monitoring
    page. Each connection is pinged (within ``timeout_seconds``) and judged by the criteria: an
    unhealthy one holding makes it ``UNHEALTHY``, else a degrading one ``DEGRADED``, else it is
    ``HEALTHY``. Keep one object: the criteria counting "since the last check" read what the pools
    did since its previous run.

    ``run()`` raises nothing about the database - a failed ping is a status; it raises only for no
    current context or a configuration naming no such connection.

    Args:
        timeout_seconds: How long a ping waits - above 0, at most 60.
        degraded_when: The criteria making a connection degraded - by default some task waits for a
            connection, or 90% of a pool is in use.
        unhealthy_when: The criteria making a connection unhealthy - by default its ping failed.
        criteria_by_connection: Other criteria for some connections, by name.
        connections: The connections checked - every connection of the configuration when None.
        check_direct: Whether the server past a connection's transaction pooler (``direct_host``) is
            pinged too.

    Raises:
        ValueError: ``timeout_seconds`` is out of range.
        ConfigurationError: A criterion reads the measured waits while ``PoolMetrics`` is off.
    """

    def __init__(
        self,
        *,
        timeout_seconds: float = DEFAULT_HEALTH_TIMEOUT_SECONDS,
        degraded_when: Sequence[HealthCriterion] | None = None,
        unhealthy_when: Sequence[HealthCriterion] | None = None,
        criteria_by_connection: Mapping[str, ConnectionCriteria] | None = None,
        connections: Sequence[str] | None = None,
        check_direct: bool = False,
    ) -> None:
        self.timeout_seconds = HealthCriterion.get_checked_amount(
            "timeout_seconds", timeout_seconds, MAX_HEALTH_TIMEOUT_SECONDS
        )
        self.degraded_when = tuple(
            [WaitingRequests(at_least=1), PoolSaturation(ratio=DEFAULT_POOL_SATURATION_RATIO)]
            if degraded_when is None
            else degraded_when
        )
        self.unhealthy_when = tuple([PingFailed()] if unhealthy_when is None else unhealthy_when)
        self.criteria_by_connection: dict[str, tuple[tuple[HealthCriterion, ...], tuple[HealthCriterion, ...]]] = {}
        for name, criteria in (criteria_by_connection or {}).items():
            self.criteria_by_connection[name] = (
                self.degraded_when if criteria.degraded_when is None else tuple(criteria.degraded_when),
                self.unhealthy_when if criteria.unhealthy_when is None else tuple(criteria.unhealthy_when),
            )
        self.connections = None if connections is None else tuple(connections)
        self.check_direct = check_direct
        every_criterion = [*self.degraded_when, *self.unhealthy_when]
        for degraded_when_of_connection, unhealthy_when_of_connection in self.criteria_by_connection.values():
            every_criterion += [*degraded_when_of_connection, *unhealthy_when_of_connection]
        for criterion in every_criterion:
            if criterion.requires_pool_metrics and not PoolMetrics.enabled:
                raise ConfigurationError(POOL_METRICS_REQUIRED_MESSAGE.format(criterion=repr(criterion)))
        #: What each pool's counters and measured waits were at the previous run - the clients are
        #: held weakly, a closed one drops out.
        self.readings: weakref.WeakKeyDictionary[DatabaseClient, tuple[int, int, int, int]] = (
            weakref.WeakKeyDictionary()
        )

    async def run(self, *, ping: bool = True) -> HealthReport:
        """Checks the connections, all at once.

        Args:
            ping: Whether each connection is pinged - without, no pool is opened and only the pools'
                state is judged.

        Returns:
            The report.

        Raises:
            ConfigurationError: No context is active, or the check names a connection the
                configuration lacks.
        """
        handler = Connections.current()
        configured_names = handler.aliases()
        unknown_names = sorted(({*self.criteria_by_connection, *(self.connections or ())}) - set(configured_names))
        if unknown_names:
            raise ConfigurationError(UNKNOWN_HEALTH_CONNECTION_MESSAGE.format(names=unknown_names))
        names = list(self.connections) if self.connections is not None else configured_names
        healths = await asyncio.gather(*(self.check_connection(handler, name, ping) for name in names))
        return HealthReport(datetime.now(UTC), dict(zip(names, healths, strict=True)))

    async def check_connection(self, handler: ConnectionHandler, name: str, ping: bool) -> ConnectionHealth:
        """Checks one connection.

        Args:
            handler: The connection handler of the current context.
            name: The connection.
            ping: Whether it is pinged.

        Returns:
            How usable it is.
        """
        ping_result = server_ping = None
        if ping:
            client = handler.get_main(name)
            ping_result = await self.ping(client)
            if self.check_direct:
                server_ping = await self.ping_server(client)
        pools = self.get_pool_changes(handler, name)
        connection_check = ConnectionCheck(name, ping_result, server_ping, pools)
        degraded_when, unhealthy_when = self.criteria_by_connection.get(
            name, (self.degraded_when, self.unhealthy_when)
        )
        status = HealthStatus.UNHEALTHY
        reasons = HealthCheck.get_reasons(unhealthy_when, connection_check)
        if not reasons:
            reasons = HealthCheck.get_reasons(degraded_when, connection_check)
            status = HealthStatus.DEGRADED if reasons else HealthStatus.HEALTHY
        failed_ping = next((result for result in (ping_result, server_ping) if result and not result.succeeded), None)
        return ConnectionHealth(
            status,
            None if ping_result is None else ping_result.latency_ms,
            tuple(reasons),
            None if failed_ping is None else failed_ping.error_type,
            tuple(pool.status for pool in pools),
        )

    @staticmethod
    def get_reasons(criteria: Sequence[HealthCriterion], connection_check: ConnectionCheck) -> list[str]:
        """The reasons of the criteria holding for a connection.

        Args:
            criteria: The criteria.
            connection_check: What was seen of the connection.

        Returns:
            The reasons - empty when none holds.
        """
        return [reason for criterion in criteria if (reason := criterion.check(connection_check)) is not None]

    async def ping(self, client: DatabaseClient) -> PingResult:
        """Pings a client - an error of any type is a failed ping, never raised.

        Args:
            client: The client.

        Returns:
            How it went.
        """
        try:
            return await ConnectionPing.run(client, self.timeout_seconds)
        except Exception as error:  # a health check reports, it never raises about the database
            return PingResult(False, None, type(error).__name__)

    async def ping_server(self, client: DatabaseClient) -> PingResult | None:
        """Pings the server past a client's transaction pooler.

        Args:
            client: The connection's client.

        Returns:
            How it went - None for a connection going straight to the server.
        """
        try:
            server_client = client.get_server_client()
        except Exception as error:  # no direct way to the server is a failed ping
            return PingResult(False, None, type(error).__name__)
        if server_client is client:
            return None
        return await self.ping(server_client)

    def get_pool_changes(self, handler: ConnectionHandler, name: str) -> tuple[PoolChange, ...]:
        """The open pools of a connection, with what they did since the previous run.

        Args:
            handler: The connection handler of the current context.
            name: The connection.

        Returns:
            The pools, by role and schema.
        """
        changes = []
        for client in PoolRegistry.get_clients():
            if (
                client.connection_handler is not handler
                or client.connection_alias != name
                or not client.features.supports_pool_status
            ):
                continue
            status = client.get_pool_status()
            if status is None:
                continue
            previous_taken, previous_timeouts, previous_failures, wait_cursor = self.readings.get(client, (0, 0, 0, 0))
            wait_cursor, waits, _ = client.pool_statistics.get_waits_since(wait_cursor)
            self.readings[client] = (
                status.acquire_count,
                status.acquire_timeouts,
                status.connect_failures,
                wait_cursor,
            )
            changes.append(
                PoolChange(
                    status,
                    max(0, status.acquire_count - previous_taken),
                    max(0, status.acquire_timeouts - previous_timeouts),
                    max(0, status.connect_failures - previous_failures),
                    tuple(waits),
                )
            )
        return tuple(sorted(changes, key=lambda change: (change.status.role, change.status.schema or "")))
