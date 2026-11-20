from __future__ import annotations

import threading
import weakref
from collections.abc import Iterator
from typing import TYPE_CHECKING, ClassVar

if TYPE_CHECKING:
    from hare.core.connections.connection_handler import ConnectionHandler
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.instrumentation.declarations import PoolStatus


class PoolRegistry:
    """The clients of the process with an open pool of connections - a client is added when it opens
    its pool and removed when it closes it. The OpenTelemetry metrics read it from their exporter's
    thread, where no ``HareContext`` is current; a context's own pools are the ones of its connection
    handler. A list of live objects, not a cache: nothing in it is ever computed.
    """

    clients: ClassVar[weakref.WeakSet[DatabaseClient]] = weakref.WeakSet()
    lock: ClassVar[threading.Lock] = threading.Lock()

    @classmethod
    def add(cls, client: DatabaseClient) -> None:
        """Adds a client that opened its pool.

        Args:
            client: The client.
        """
        with cls.lock:
            cls.clients.add(client)

    @classmethod
    def remove(cls, client: DatabaseClient) -> None:
        """Removes a client that closed its pool.

        Args:
            client: The client.
        """
        with cls.lock:
            cls.clients.discard(client)

    @classmethod
    def get_clients(cls) -> list[DatabaseClient]:
        """The clients with an open pool.

        Returns:
            The clients.
        """
        with cls.lock:
            return list(cls.clients)

    @classmethod
    def iter_statuses(
        cls, *, handler: ConnectionHandler | None = None, alias: str | None = None
    ) -> Iterator[tuple[DatabaseClient, PoolStatus]]:
        """Yield a snapshot's available statuses, optionally restricted to one handler or alias."""
        for client in cls.get_clients():
            if handler is not None and client.connection_handler is not handler:
                continue
            if alias is not None and client.connection_alias != alias:
                continue
            if client.features.supports_pool_status and (status := client.get_pool_status()) is not None:
                yield client, status
