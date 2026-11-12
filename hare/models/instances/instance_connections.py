from __future__ import annotations

from typing import TYPE_CHECKING

from hare.core.connections.connections import Connections
from hare.core.connections.model_connection_choice import ModelConnectionChoice
from hare.core.hare_context import HareContext

if TYPE_CHECKING:
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.models.model import Model


class InstanceConnections:
    """The connection an instance is read from and written to: the one it was loaded through,
    remembered on it, or the one the router or the model's configuration chooses."""

    @staticmethod
    def get_connection_for_instance(
        obj: Model,
        for_write: bool = False,
        model: type[Model] | None = None,
        *,
        using: str | DatabaseClient | None = None,
    ) -> DatabaseClient:
        """The connection for an operation on this obj or a relation of it: an explicit choice, the router's choice,
        else the connection the obj came from, else the model's default one. A related model
        with another default connection keeps its own.

        Args:
            obj: The model obj.
            for_write: Whether the connection is chosen for a write.
            model: The model queried - this obj's own when omitted.
            using: An explicit alias or client, checked against the queried model's tenancy too.

        Returns:
            The chosen connection.
        """
        queried_model = model if model is not None else type(obj)
        connection = Connections.get_client(using)
        if connection is not None:
            return ModelConnectionChoice.get_checked(queried_model, connection)
        connection_alias = obj._connection_alias
        if connection_alias is None or queried_model._meta.default_connection != obj._meta.default_connection:
            return queried_model.get_connection(for_write=for_write)
        context = HareContext.require_current()
        router = context.router
        if router.has_routers:
            connection = ModelConnectionChoice.get_routed(context, queried_model, for_write)
            if connection is not None:
                return ModelConnectionChoice.get_checked(queried_model, connection)
        connection = ModelConnectionChoice.get_instance_fallback(
            context, queried_model, type(obj), connection_alias, for_write
        )
        if connection is None:
            connection = ModelConnectionChoice.get_default(context, queried_model)
        return ModelConnectionChoice.get_checked(queried_model, connection)

    @staticmethod
    def is_routed(model: type[Model], for_write: bool = False) -> bool:
        """Whether the router chooses the connection for this model.

        Args:
            model: The model.
            for_write: Whether the connection is chosen for a write.

        Returns:
            True when a router has an opinion for this model.
        """
        context = HareContext.require_current()
        router = context.router
        if not router.has_routers:
            return False
        connection = ModelConnectionChoice.get_routed(context, model, for_write)
        return connection is not None
