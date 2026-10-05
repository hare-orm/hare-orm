from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any

from hare.core.connections.connections import Connections
from hare.core.routing.primary_reads import PrimaryReads
from hare.core.routing.written_connections import WrittenConnections
from hare.exceptions import ConfigurationError

if TYPE_CHECKING:
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.models import Model


class ConnectionRouter:
    """Dispatches a model to a specific DB connection by asking each configured router in turn,
    Django-style, for the first one that returns a connection alias for the given action. A read
    after a write of the same task goes to the connection written through - a replica may not
    have the write yet (``WrittenConnections``, ``Routing.using_primary()``)."""

    def __init__(self) -> None:
        self._routers: list[object] | None = None
        #: How long after a write the reads of the written connection's models stay on it, None
        #: for the rest of the task (the config's ``read_your_writes_seconds``).
        self.read_your_writes_seconds: float | None = None

    @property
    def has_routers(self) -> bool:
        """Whether any router is configured - without one, every model uses its own connection."""
        return bool(self._routers)

    def init_routers(self, routers: Sequence[Callable[[], object]]) -> None:
        """Instantiates every configured router class.

        Args:
            routers: Router classes, each constructible without arguments.

        Raises:
            ConfigurationError: A router class can't be instantiated without arguments.
        """
        router_instances = []
        for router_class in routers:
            try:
                router_instances.append(router_class())
            except TypeError as error:
                raise ConfigurationError(
                    f"Can't instantiate router {router_class!r} - a router class must take no constructor "
                    f"arguments: {error}"
                ) from error
        self._routers = router_instances
        if router_instances:
            WrittenConnections.is_recording = True

    def _get_routers_choice(self, model: type[Model], action: str) -> Any:
        for router in self._routers or []:
            try:
                method = getattr(router, action)
            except AttributeError:
                # If the router doesn't have a method, skip to the next one.
                pass
            else:
                chosen_connection = method(model)
                if chosen_connection:
                    return chosen_connection
        return None

    def _route_connection(self, model: type[Model], action: str) -> DatabaseClient | None:
        connection_alias = self._get_routers_choice(model, action)
        if connection_alias is None:
            # No configured router returned anything for this action - genuinely "no router
            # applies", the caller falls back to the model's default connection. Distinct from
            # the case below: only THIS case is a silent None.
            return None
        # An alias a router returned must resolve - a broken router raises instead of falling back
        # to the default connection.
        return Connections.get(connection_alias)

    def db_for_read(self, model: type[Model]) -> DatabaseClient | None:
        """Returns the connection to use for reading `model`, or None if no router applies - the
        connection `model` is written through when the task wrote there (within
        ``read_your_writes_seconds``) or runs inside ``Routing.using_primary()``."""
        if not self._routers:
            return None
        written = WrittenConnections.current.get()
        reads_primary = PrimaryReads.active.get()
        if written is not None or reads_primary:
            write_connection = self._route_connection(model, "db_for_write")
            if write_connection is not None and (
                reads_primary
                or (
                    written is not None
                    and written.reads_from(write_connection.connection_alias, self.read_your_writes_seconds)
                )
            ):
                return write_connection
        return self._route_connection(model, "db_for_read")

    def db_for_write(self, model: type[Model]) -> DatabaseClient | None:
        """Returns the connection to use for writing `model`, or None if no router applies - the
        write itself records the connection (``WrittenConnections``), not this choice: a check of
        what the connection supports writes nothing."""
        if not self._routers:
            return None

        return self._route_connection(model, "db_for_write")
