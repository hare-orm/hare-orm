from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from hare.models.deletion.cascade.deletion_graph import DeletionGraph
from hare.models.tenancy.tenancy import Tenancy
from hare.models.tenancy.tenant_schemas import TenantSchemas

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.models import Model


class TenantSchemaVisits:
    """The tenant schemas a delete or a restore of rows every tenant shares reaches besides the one
    it runs in - each visited in turn inside its transaction, which then runs in that schema."""

    @staticmethod
    def visits_schemas(model: type[Model], connection: DatabaseClient | None, *, leaves_constrained: bool) -> bool:
        """Whether a delete or a restore of ``model`` rows on ``connection`` visits the other tenant
        schemas.

        Args:
            model: The model of the root rows.
            connection: The connection it runs on.
            leaves_constrained: Whether it is a hard delete leaving the relations the database
                enforces to it - visiting only for the work the database's own cascade doesn't do.

        Returns:
            True in a transaction of a connection with a schema per tenant, for a model kept once
            for every tenant whose rows ``Meta.tenant_schema`` models point at.
        """
        return (
            connection is not None
            and connection.is_transaction_client
            and connection.tenant_schema_template is not None
            and (
                DeletionGraph.has_tenant_schema_work(model)
                if leaves_constrained
                else DeletionGraph.reaches_tenant_schema_models(model)
            )
        )

    @staticmethod
    async def get_other_schemas(connection: DatabaseClient) -> list[tuple[str, str]]:
        """The tenant schemas besides the one the transaction runs in.

        Args:
            connection: The transaction.

        Returns:
            ``(schema name, tenant)`` pairs, sorted by the schema name.
        """
        return [
            (schema_name, tenant)
            for schema_name, tenant in await TenantSchemas.get_schemas(connection)
            if schema_name != connection.tenant_schema
        ]

    @staticmethod
    @asynccontextmanager
    async def visit(connection: DatabaseClient, schema_name: str, tenant: str) -> AsyncIterator[None]:
        """Runs the block in another tenant's schema, inside the transaction - with that tenant's
        scope active. An error leaves the schema to the rollback that follows it.

        Args:
            connection: The transaction.
            schema_name: The tenant's schema.
            tenant: The tenant.

        Yields:
            Nothing.
        """
        wrappers = []
        client = connection
        while client.is_transaction_client:
            wrappers.append((client, client.tenant_schema))
            client = client._parent
        # Local import: the introspector's modules import the models.
        from hare.inspectdb.introspection.database_catalog import DatabaseCatalog

        table_names = frozenset(await DatabaseCatalog.get_table_names(connection, schema=schema_name))
        search_path = await connection.enter_tenant_schema(schema_name)
        for wrapper, _tenant_schema in wrappers:
            wrapper.tenant_schema = schema_name
        tables_token = DeletionGraph.visited_schema_tables.set(table_names)
        try:
            with Tenancy.scope(tenant):
                yield
        finally:
            DeletionGraph.visited_schema_tables.reset(tables_token)
            for wrapper, tenant_schema in wrappers:
                wrapper.tenant_schema = tenant_schema
        await connection.leave_tenant_schema(search_path)
