from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from hare.core.connections.connections import Connections
from hare.exceptions import UnSupportedError
from hare.models.deletion.cascade.deletion_graph import DeletionGraph
from hare.models.deletion.cascade.deletion_scope import DeletionScope
from hare.models.deletion.constants import TENANT_VISIT_REQUIRES_SAVEPOINT
from hare.models.tenancy.tenancy import Tenancy
from hare.models.tenancy.tenant_schemas import TenantSchemas
from hare.query.scopes.row_scope_binding import RowScopeBinding

if TYPE_CHECKING:
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
    async def visit(
        connection: DatabaseClient,
        schema_name: str,
        tenant: str,
    ) -> AsyncGenerator[DeletionScope]:
        """Visits a schema through a dedicated savepoint client and task-local binding.

        The enclosing wrappers retain their schema throughout. On success the search path
        is restored before releasing the savepoint; on failure rollback restores it. The
        yielded scope supplies the client to every lookup and write of this visit.
        """
        from hare.inspectdb.introspection.database_catalog import DatabaseCatalog

        if not connection.features.supports_savepoints:
            raise UnSupportedError(TENANT_VISIT_REQUIRES_SAVEPOINT)
        async with connection._in_transaction() as schema_connection:
            tables = frozenset(await DatabaseCatalog.get_table_names(schema_connection, schema=schema_name))
            previous_schemas = await schema_connection.enter_tenant_schema(schema_name)
            schema_connection.tenant_schema = schema_name
            scope = DeletionScope(schema_connection, schema_name, tenant, tables)
            connections = schema_connection.connection_handler
            if connections is None:
                connections = Connections.current()
                schema_connection.connection_handler = connections
            connection_token = connections.set(schema_connection.connection_alias, schema_connection)
            scope_token = DeletionScope.current.set(scope)
            try:
                with Tenancy.scope(tenant):
                    binding_token = RowScopeBinding.current.set(RowScopeBinding())
                    try:
                        yield scope
                    finally:
                        RowScopeBinding.current.reset(binding_token)
            finally:
                DeletionScope.current.reset(scope_token)
                connection_token.reset()
            await schema_connection.leave_tenant_schema(previous_schemas)
