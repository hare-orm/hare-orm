from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import TYPE_CHECKING, Any, TypeVar

if TYPE_CHECKING:
    from hare.dialects.base.client.database_client import DatabaseClient

    DBConfigType = dict[str, Any]
from hare.core.connection_handler import ConnectionHandler

TaskResult = TypeVar("TaskResult")


class Connections:
    """The connections of the current context - ``Connections.get(alias)`` is
    ``HareContext.require_current().connections.get(alias)``. Every method needs an active
    ``HareContext`` and raises a clear error without one.
    """

    @staticmethod
    def get(alias: str) -> DatabaseClient:
        """
        Get a database connection by alias from the current context.

        This is a convenience function. Prefer accessing connections directly
        via context: `ctx.connections.get(alias)`

        Args:
            alias: The connection alias (e.g., "default").

        Raises:
            ConfigurationError: If no context is active or connection not found.
        """
        return Connections.current().get(alias)

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
    def aliases() -> list[str]:
        """
        Returns every connection alias configured on the current context.

        This is a convenience function. Prefer accessing connections directly
        via context: `ctx.connections.aliases()`

        Raises:
            ConfigurationError: If no context is active.
        """
        return Connections.current().aliases()

    @staticmethod
    def current() -> ConnectionHandler:
        """
        Get the ConnectionHandler from the current context.

        This is a convenience function. Prefer accessing connections directly
        via context: `ctx.connections`

        Raises:
            ConfigurationError: If no context is active.
        """
        from hare.core.context import HareContext

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
        from hare.core.context import HareContext

        context = HareContext.get_current()
        if context is None:
            return asyncio.ensure_future(coroutine)
        return context.connections.create_task_outside_transactions(coroutine)
