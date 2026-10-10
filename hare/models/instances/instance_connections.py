from __future__ import annotations

from typing import TYPE_CHECKING

from hare.core.hare_context import HareContext

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.models.model import Model


class InstanceConnections:
    """The connection an instance is read from and written to: the one it was loaded through,
    remembered on it, or the one the router or the model's configuration chooses."""

    @staticmethod
    def remember_connection(obj: Model, connection: DatabaseClient) -> None:
        """Records the connection this obj was loaded from or saved to.

        Args:
            obj: The model obj.
            connection: The connection.
        """
        object.__setattr__(obj, "_connection_alias", connection.connection_alias)

    @staticmethod
    def get_connection_for_instance(
        obj: Model, for_write: bool = False, model: type[Model] | None = None
    ) -> DatabaseClient:
        """The connection for an operation on this obj or a relation of it: the router's choice,
        else the connection the obj came from, else the model's default one. A related model
        with another default connection keeps its own.

        Args:
            obj: The model obj.
            for_write: Whether the connection is chosen for a write.
            model: The model queried - this obj's own when omitted.

        Returns:
            The chosen connection.
        """
        queried_model = model if model is not None else type(obj)
        connection_alias = obj._connection_alias
        if connection_alias is None or queried_model._meta.default_connection != obj._meta.default_connection:
            return queried_model.get_connection(for_write=for_write)
        context = HareContext.require_current()
        router = context.router
        if router.has_routers:
            connection = router.db_for_write(queried_model) if for_write else router.db_for_read(queried_model)
            if connection is not None:
                return connection
            if queried_model is not type(obj) and InstanceConnections.is_routed(type(obj), for_write):
                return queried_model.get_connection(for_write=for_write)
        return context.connections.get(connection_alias)

    @staticmethod
    def is_routed(model: type[Model], for_write: bool = False) -> bool:
        """Whether the router chooses the connection for this model.

        Args:
            model: The model.
            for_write: Whether the connection is chosen for a write.

        Returns:
            True when a router has an opinion for this model.
        """
        router = HareContext.require_current().router
        if not router.has_routers:
            return False
        connection = router.db_for_write(model) if for_write else router.db_for_read(model)
        return connection is not None
