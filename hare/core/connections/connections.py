from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import TYPE_CHECKING, Any, TypeVar

if TYPE_CHECKING:
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.instrumentation.declarations import PoolStatus

    DBConfigType = dict[str, Any]
from hare.core.connections.connection_handler import ConnectionHandler
from hare.instrumentation.pools.pool_registry import PoolRegistry

TaskResult = TypeVar("TaskResult")


class Connections:
    """The connections of the current context - ``Connections.get(connection_alias)`` is
    ``HareContext.require_current().connections.get(connection_alias)``. Every method needs an active
    ``HareContext`` and raises a clear error without one.
    """

    @staticmethod
    def get(connection_alias: str) -> DatabaseClient:
        """
        Get a database connection by connection_alias from the current context.

        This is a convenience function. Prefer accessing connections directly
        via context: `context.connections.get(connection_alias)`

        Args:
            connection_alias: The connection connection_alias (e.g., "default").

        Raises:
            ConfigurationError: If no context is active or connection not found.
        """
        return Connections.current().get(connection_alias)

    @staticmethod
    def get_client(using: str | DatabaseClient | None) -> DatabaseClient | None:
        """The connection a ``using=`` argument names.

        Args:
            using: A connection's alias, a connection's client, or None.

        Returns:
            The alias's connection in the current context, the client itself, or None.

        Raises:
            ConfigurationError: If ``using`` is an alias and no context is active or it isn't found.
        """
        if isinstance(using, str):
            return Connections.current().get(using)
        return using

    @staticmethod
    def get_pool_statuses(connection_alias: str | None = None) -> list[PoolStatus]:
        """What every open pool of the current context holds and has done - of each connection's own
        client, its tenant schemas' clients, the client past a transaction pooler and the clients of
        their own (``PoolRole``). A client reporting no pool status (``Features.supports_pool_status``)
        has none in the list.

        Args:
            connection_alias: Only the pools of this connection - every connection's when None.

        Returns:
            The statuses, by connection, role and schema.

        Raises:
            ConfigurationError: If no context is active.
        """
        handler = Connections.current()
        statuses = []
        for client in PoolRegistry.get_clients():
            if client.connection_handler is not handler or not client.features.supports_pool_status:
                continue
            if connection_alias is not None and client.connection_alias != connection_alias:
                continue
            status = client.get_pool_status()
            if status is not None:
                statuses.append(status)
        return sorted(statuses, key=lambda status: (status.connection_alias, status.role, status.schema or ""))

    @staticmethod
    def aliases() -> list[str]:
        """
        Returns every connection alias configured on the current context.

        This is a convenience function. Prefer accessing connections directly
        via context: `context.connections.aliases()`

        Raises:
            ConfigurationError: If no context is active.
        """
        return Connections.current().aliases()

    @staticmethod
    def current() -> ConnectionHandler:
        """
        Get the ConnectionHandler from the current context.

        This is a convenience function. Prefer accessing connections directly
        via context: `context.connections`

        Raises:
            ConfigurationError: If no context is active.
        """
        return HareContext.require_current().connections

    @staticmethod
    async def reconnect() -> None:
        """Closes every connection and pool of the current context, keeping its configuration, so
        the next query opens new ones - for a database replaced under a running application: a
        SQLite file swapped for a copy, a PostgreSQL database restored from a dump. Call it with
        no transaction open, once the database is back; a query started before it runs on the
        old connection. Each process reconnects its own connections.

        Raises:
            ConfigurationError: If no context is active.
        """
        await Connections.current().close_all(discard=False)

    @staticmethod
    def create_task_outside_transactions(coroutine: Coroutine[Any, Any, TaskResult]) -> asyncio.Task[TaskResult]:
        """Starts ``coroutine`` as a task that sees every alias's shared, non-transactional client
        of the current context - a plain task when no context is active.

        Args:
            coroutine: The coroutine to run.

        Returns:
            The started task.
        """
        context = HareContext.get_current()
        if context is None:
            return asyncio.ensure_future(coroutine)
        return context.connections.create_task_outside_transactions(coroutine)


# Imported last: the context module imports this one.
from hare.core.hare_context import HareContext  # noqa: E402
