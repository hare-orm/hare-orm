from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from hare.core.resource_completion import ResourceCompletion
from hare.dialects.base.client.constants import DISCARDED_CLIENT_MESSAGE
from hare.exceptions import DBConnectionError
from hare.instrumentation.enums import PoolRole

if TYPE_CHECKING:
    from hare.dialects.base.client.database_client import DatabaseClient


class TenantClients:
    """Creates, selects and closes the independent clients used by schema-per-tenant connections."""

    @staticmethod
    def get_tenant_client(source_client: DatabaseClient) -> DatabaseClient:
        """The client the active tenant scope works through - the client of the tenant's schema when
        the connection has a schema per tenant and the scope is one tenant, else this client.

        Returns:
            The client.
        """
        if source_client.tenant_schema is not None or source_client.tenant_schema_template is None:
            return source_client
        # Local import: the tenant schemas import the models, which import the dialects.
        from hare.models.tenancy.tenant_schemas import TenantSchemas

        schema_name = TenantSchemas.get_active_schema_name(source_client)
        if schema_name is None:
            return source_client
        return source_client.get_schema_client(schema_name)

    @staticmethod
    def get_schema_client(source_client: DatabaseClient, schema_name: str) -> DatabaseClient:
        """The client of one tenant's schema, made on its first use.

        Args:
            schema_name: The tenant's schema.

        Returns:
            The client.

        Raises:
            UnSupportedError: The database has no schemas per tenant.
            DBConnectionError: This client was closed and dropped, and nothing replaced it - a client made
                now would open connections nothing closes.
        """
        tenant_client = source_client.tenant_clients.get(schema_name)
        if tenant_client is None:
            if source_client.discarded:
                successor = source_client.get_successor()
                if successor is None:
                    raise DBConnectionError(DISCARDED_CLIENT_MESSAGE.format(alias=source_client.connection_alias))
                return successor.get_schema_client(schema_name)
            tenant_client = source_client.tenant_clients[schema_name] = TenantClients._create_client(
                source_client, {}, source_client.get_tenant_client_settings(schema_name), schema_name
            )
            tenant_client.pool_role = PoolRole.TENANT_SCHEMA
        return tenant_client

    @staticmethod
    def create_independent_client(
        source_client: DatabaseClient, settings: Mapping[str, Any] | None = None
    ) -> DatabaseClient:
        """A new connection to this client's database, apart from it and in the same tenant schema.
        The caller closes it.

        Args:
            settings: Settings replacing the connection's own.

        Returns:
            The client.
        """
        return TenantClients._create_client(
            source_client, settings or {}, source_client.tenant_client_settings, source_client.tenant_schema
        )

    @staticmethod
    def _create_client(
        source_client: DatabaseClient,
        settings: Mapping[str, Any],
        tenant_client_settings: Mapping[str, Any],
        tenant_schema: str | None,
    ) -> DatabaseClient:
        """A new connection to this client's database.

        Args:
            settings: Settings replacing the connection's own.
            tenant_client_settings: The settings of the tenant schema it works in.
            tenant_schema: The tenant schema it works in, None for none.

        Returns:
            The client.
        """
        # Local import: the connections import the dialects.
        from hare.core.connections.connections import Connections

        connections = (
            source_client.connection_handler if source_client.connection_handler is not None else Connections.current()
        )
        client = connections.create_independent(
            source_client.connection_alias,
            {**settings, **tenant_client_settings},
            config=source_client.connection_config,
        )
        client.tenant_schema_template = source_client.tenant_schema_template
        client.tenant_schema = tenant_schema
        client.tenant_client_settings = tenant_client_settings
        return client

    @staticmethod
    async def close_tenant_clients(source_client: DatabaseClient) -> None:
        """Close every tenant client, including after a sibling failure or caller cancellation."""
        tenant_clients = list(source_client.tenant_clients.values())
        source_client.tenant_clients.clear()
        await ResourceCompletion.wait_all(*(tenant_client.close() for tenant_client in tenant_clients))
