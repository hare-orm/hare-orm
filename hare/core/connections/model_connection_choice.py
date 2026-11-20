from __future__ import annotations

from typing import TYPE_CHECKING

from hare.core.connections.messages import FOREIGN_CONTEXT_CLIENT_MESSAGE, MISSING_DEFAULT_CONNECTION_MESSAGE
from hare.exceptions import ConfigurationError

if TYPE_CHECKING:
    from hare.core.hare_context import HareContext
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.models.model_binding import ModelBinding


class ModelConnectionChoice:
    """One connection-selection algorithm with explicit application and structural inputs."""

    @staticmethod
    def get(context: HareContext, binding: ModelBinding, for_write: bool) -> DatabaseClient:
        return ModelConnectionChoice.get_checked(
            binding, ModelConnectionChoice.get_unchecked(context, binding, for_write), context=context
        )

    @staticmethod
    def check_context(context: HareContext | None, connection: DatabaseClient) -> None:
        """Refuse another application's client without retaining resources on a model graph.

        Detached schema compilation has no application owner. Standalone clients without
        a handler also have no conflicting owner; transactions, independent clients and
        tenant clients retain the handler of their originating application.
        """
        handler = connection.connection_handler
        if context is not None and handler is not None and handler is not context.connections:
            raise ConfigurationError(FOREIGN_CONTEXT_CLIENT_MESSAGE)

    @staticmethod
    def get_unchecked(context: HareContext, binding: ModelBinding, for_write: bool) -> DatabaseClient:
        connection = ModelConnectionChoice.get_routed(context, binding, for_write)
        return ModelConnectionChoice.get_default(context, binding) if connection is None else connection

    @staticmethod
    def get_routed(context: HareContext, binding: ModelBinding, for_write: bool) -> DatabaseClient | None:
        router = context.router
        return router.db_for_write(binding) if for_write else router.db_for_read(binding)

    @staticmethod
    def has_route(context: HareContext, binding: ModelBinding, for_write: bool = False) -> bool:
        """Whether a configured router chooses a client for the binding."""
        return context.router.has_routers and ModelConnectionChoice.get_routed(context, binding, for_write) is not None

    @staticmethod
    def get_default(context: HareContext, binding: ModelBinding) -> DatabaseClient:
        default_connection = binding.default_connection
        if default_connection is None:
            raise ConfigurationError(MISSING_DEFAULT_CONNECTION_MESSAGE.format(model_name=binding._model.__name__))
        return context.connections.get(default_connection)

    @staticmethod
    def get_metadata_connection(
        context: HareContext, binding: ModelBinding, *, for_write: bool = False
    ) -> DatabaseClient:
        """Compare configured routes using base clients, without selecting an ambient tenant."""
        get_connection = context.connections.get_main
        model = binding._model
        connection = (
            context.router.db_for_write(binding, get_connection=get_connection)
            if for_write
            else context.router.db_for_read(binding, get_connection=get_connection)
        )
        if connection is not None:
            return connection
        if binding.default_connection is None:
            raise ConfigurationError(MISSING_DEFAULT_CONNECTION_MESSAGE.format(model_name=model.__name__))
        return get_connection(binding.default_connection)

    @staticmethod
    def get_instance_fallback(
        context: HareContext,
        binding: ModelBinding,
        source_binding: ModelBinding | None,
        connection_alias: str,
        for_write: bool,
    ) -> DatabaseClient | None:
        if source_binding is not None:
            if binding.default_connection != source_binding.default_connection:
                return None
            if source_binding is not binding and ModelConnectionChoice.has_route(context, source_binding, for_write):
                return None
        return context.connections.get(connection_alias)

    @staticmethod
    def get_related_write_client(
        context: HareContext,
        binding: ModelBinding,
        source_binding: ModelBinding,
        connection: DatabaseClient | None,
    ) -> DatabaseClient | None:
        """Preserve the exact client when the models share a write route."""
        if connection is None or binding is source_binding:
            return connection
        if binding.default_connection != source_binding.default_connection:
            return None
        source = ModelConnectionChoice.get_metadata_connection(context, source_binding, for_write=True)
        target = ModelConnectionChoice.get_metadata_connection(context, binding, for_write=True)
        return connection if source.connection_alias == target.connection_alias else None

    @staticmethod
    def get_checked(
        binding: ModelBinding, connection: DatabaseClient, *, context: HareContext | None
    ) -> DatabaseClient:
        ModelConnectionChoice.check_context(context, connection)
        if connection.sets_tenants:
            from hare.models.tenancy.tenant_row_level_security import TenantRowLevelSecurity

            TenantRowLevelSecurity.check_transaction_scope(connection)
        if binding.checks_tenant_client:
            binding.check_tenant_client(connection)
        return connection
