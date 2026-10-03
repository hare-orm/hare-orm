from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any

from hare.core.connections import Connections
from hare.exceptions import ConfigurationError

if TYPE_CHECKING:
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.models import Model


class ConnectionRouter:
    """Dispatches a model to a specific DB connection by asking each configured router in turn,
    Django-style, for the first one that returns a connection alias for the given action."""

    def __init__(self) -> None:
        self._routers: list[object] | None = None

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
            except TypeError as exc:
                raise ConfigurationError(
                    f"Can't instantiate router {router_class!r} - a router class must take no constructor "
                    f"arguments: {exc}"
                ) from exc
        self._routers = router_instances

    def _router_func(self, model: type[Model], action: str) -> Any:
        for r in self._routers or []:
            try:
                method = getattr(r, action)
            except AttributeError:
                # If the router doesn't have a method, skip to the next one.
                pass
            else:
                chosen_db = method(model)
                if chosen_db:
                    return chosen_db

    def _db_route(self, model: type[Model], action: str) -> DatabaseClient | None:
        alias = self._router_func(model, action)
        if alias is None:
            # No configured router returned anything for this action - genuinely "no router
            # applies", the caller falls back to the model's default connection. Distinct from
            # the case below: only THIS case is a silent None.
            return None
        # An alias a router returned must resolve - a broken router raises instead of falling back
        # to the default connection.
        return Connections.get(alias)

    def db_for_read(self, model: type[Model]) -> DatabaseClient | None:
        """Returns the connection to use for reading `model`, or None if no router applies."""
        if not self._routers:
            return None

        return self._db_route(model, "db_for_read")

    def db_for_write(self, model: type[Model]) -> DatabaseClient | None:
        """Returns the connection to use for writing `model`, or None if no router applies."""
        if not self._routers:
            return None

        return self._db_route(model, "db_for_write")
