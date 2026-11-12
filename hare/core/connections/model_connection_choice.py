from __future__ import annotations

from typing import TYPE_CHECKING

from hare.core.connections.messages import MISSING_DEFAULT_CONNECTION_MESSAGE
from hare.core.hare_context import HareContext
from hare.exceptions import ConfigurationError

if TYPE_CHECKING:
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.models import Model


class ModelConnectionChoice:
    """The connection a query on a model runs on: the routers' choice, else the model's default
    connection - inside an open transaction on it, the transaction's client - checked against the
    model's tenancy."""

    @staticmethod
    def get(model: type[Model], for_write: bool) -> DatabaseClient:
        """The connection a query on the model runs on now, checked against its tenancy.

        Args:
            model: The model.
            for_write: Whether the connection is chosen for a write.

        Returns:
            The connection.

        Raises:
            ConfigurationError: No Hare context is active, the model has no default connection, or its
                tenancy needs a setting of the connection it lacks.
            QueryError: The model's tenancy needs a single active tenant, or a transaction that set its
                tenants.
        """
        return ModelConnectionChoice.get_checked(model, ModelConnectionChoice.get_unchecked(model, for_write))

    @staticmethod
    def get_unchecked(model: type[Model], for_write: bool) -> DatabaseClient:
        """The connection a query on the model is routed to, its tenancy unchecked.

        Args:
            model: The model.
            for_write: Whether the connection is chosen for a write.

        Returns:
            The connection.

        Raises:
            ConfigurationError: No Hare context is active, or the model has no default connection.
        """
        context = HareContext.require_current()
        connection = ModelConnectionChoice.get_routed(context, model, for_write)
        return ModelConnectionChoice.get_default(context, model) if connection is None else connection

    @staticmethod
    def get_routed(context: HareContext, model: type[Model], for_write: bool) -> DatabaseClient | None:
        """The connection the context's routers choose for the model.

        Args:
            context: The active context.
            model: The model.
            for_write: Whether the connection is chosen for a write.

        Returns:
            The connection, None when no router chooses one.
        """
        router = context.router
        return router.db_for_write(model) if for_write else router.db_for_read(model)

    @staticmethod
    def get_default(context: HareContext, model: type[Model]) -> DatabaseClient:
        """The client of the model's default connection, unrouted.

        Args:
            context: The active context.
            model: The model.

        Returns:
            The client.

        Raises:
            ConfigurationError: The model has no default connection.
        """
        default_connection = model._meta.default_connection
        if default_connection is None:
            raise ConfigurationError(MISSING_DEFAULT_CONNECTION_MESSAGE.format(model_name=model.__name__))
        return context.connections.get(default_connection)

    @staticmethod
    def get_metadata_connection(model: type[Model], *, for_write: bool = False) -> DatabaseClient:
        """The base client of a model's route, without selecting an ambient tenant.

        Relation metadata only compares which configured connections hold the two tables.
        It must remain readable outside a tenant scope and inside a transaction opened on an
        explicitly selected tenant client. Actual table access checks that selected client.
        Router ordering and read-your-writes use the same algorithm as execution.

        Args:
            model: The model whose configured connection is compared.
            for_write: Whether to compare its write route rather than its read route.
        """
        context = HareContext.require_current()
        get_connection = context.connections.get_main
        connection = (
            context.router.db_for_write(model, get_connection=get_connection)
            if for_write
            else context.router.db_for_read(model, get_connection=get_connection)
        )
        if connection is not None:
            return connection
        default_connection = model._meta.default_connection
        if default_connection is None:
            raise ConfigurationError(MISSING_DEFAULT_CONNECTION_MESSAGE.format(model_name=model.__name__))
        return get_connection(default_connection)

    @staticmethod
    def get_instance_fallback(
        context: HareContext,
        model: type[Model],
        source_model: type[Model] | None,
        connection_alias: str | None,
        for_write: bool,
    ) -> DatabaseClient | None:
        """The source instance's connection, when neither model's routing prevents inheritance.

        Call after asking the router about ``model``. The source model's decision is read for
        this execution too; no instance or earlier routing decision is retained by a queryset.
        """
        if connection_alias is None:
            return None
        if source_model is not None:
            if model._meta.default_connection != source_model._meta.default_connection:
                return None
            if (
                source_model is not model
                and context.router.has_routers
                and ModelConnectionChoice.get_routed(context, source_model, for_write) is not None
            ):
                return None
        return context.connections.get(connection_alias)

    @staticmethod
    def get_checked(model: type[Model], connection: DatabaseClient) -> DatabaseClient:
        """The connection, once the model's tenancy allows a query of it on the connection.

        Args:
            model: The model.
            connection: The connection.

        Returns:
            The connection.

        Raises:
            ConfigurationError: The tenancy needs a setting of the connection it lacks.
            QueryError: The tenancy needs a single active tenant, or a transaction that set its tenants.
        """
        if connection.sets_tenants:
            from hare.models.tenancy.tenant_row_level_security import TenantRowLevelSecurity

            TenantRowLevelSecurity.check_transaction_scope(connection)
        meta = model._meta
        if meta.checks_tenant_client:
            meta.check_tenant_client(connection)
        return connection
