from __future__ import annotations

from typing import TYPE_CHECKING

from hare.core.connections.model_connection_choice import ModelConnectionChoice
from hare.exceptions import ConfigurationError

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
            using: An explicit alias or client of the instance's context, checked against
                the queried model's tenancy too.

        Returns:
            The chosen connection.

        Raises:
            ConfigurationError: The graph is unpublished, the instance is detached, its context
                is closed, or the selected client belongs to another HareContext.
        """
        obj._binding.graph.check_published()
        context = obj._context
        if context is None:
            raise ConfigurationError("A detached model instance needs an explicitly attached application context")
        context.check_open()
        binding = obj._binding if model is None else obj._binding.graph.get_binding(model)
        connection = context.connections.get(using) if isinstance(using, str) else using
        if connection is None:
            connection_alias = obj._connection_alias
            if context.router.has_routers:
                connection = ModelConnectionChoice.get_routed(context, binding, for_write)
            if connection is None and connection_alias is not None:
                connection = ModelConnectionChoice.get_instance_fallback(
                    context, binding, obj._binding, connection_alias, for_write
                )
            if connection is None:
                connection = ModelConnectionChoice.get_default(context, binding)
        return ModelConnectionChoice.get_checked(binding, connection, context=context)
