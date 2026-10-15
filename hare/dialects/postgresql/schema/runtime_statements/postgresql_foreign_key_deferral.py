from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager, suppress
from typing import TYPE_CHECKING

from hare.dialects.postgresql.schema.constants import POSTGRESQL_DEFERRABLE_CONSTRAINT_NAME_QUERY
from hare.exceptions import HareError
from hare.models.deletion.cascade.deletion_graph import DeletionGraph
from hare.models.deletion.cascade.protect_constraint_deferral import ProtectConstraintDeferral
from hare.models.tenancy.tenant_schemas import TenantSchemas

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.models import Model


class PostgresqlForeignKeyDeferral:
    """Defers the ``on_delete=PROTECT`` foreign keys a cascade delete can run into. PostgreSQL checks a
    ``NO ACTION`` constraint at the end of every nested cascade statement, so a guarded row fails
    the delete even when the same cascade removes the guarding row later. The PROTECT constraints
    are created ``DEFERRABLE``, deferred for the delete and checked right after it.
    """

    @staticmethod
    async def get_protect_constraint_names(model: type[Model], connection: DatabaseClient) -> tuple[str, ...]:
        """The quoted, schema-qualified names of the deferrable constraints behind the PROTECT foreign
        keys a hard delete of ``model`` rows can run into - those of a ``Meta.tenant_schema`` model in
        every tenant's schema, since the delete of a row every tenant shares reaches them all - read
        from ``pg_constraint`` and cached per model, connection and tenant schemas until DDL runs or a
        relation changes.

        Args:
            model: The model rows are deleted from.
            connection: The connection the delete runs on.

        Returns:
            The names; a constraint that is missing or not deferrable is left out.
        """
        foreign_key_fields = DeletionGraph.get_cascade_protect_foreign_keys(model)
        tenant_schema_names: tuple[str, ...] = ()
        if connection.tenant_schema_template is not None and any(
            foreign_key_field.model._meta.tenant_schema for foreign_key_field in foreign_key_fields
        ):
            tenant_schema_names = tuple(schema_name for schema_name, _ in await TenantSchemas.get_schemas(connection))
        names_key = (connection.connection_alias, tenant_schema_names)
        cached_names: tuple[str, ...] | None = ProtectConstraintDeferral.CONSTRAINT_NAMES_CACHE.get_for_model(
            model, names_key
        )
        if cached_names is not None:
            return cached_names
        names: list[str] = []
        literals = connection.dialect.literals
        for foreign_key_field in foreign_key_fields:
            referencing_meta = foreign_key_field.model._meta
            referenced_meta = foreign_key_field.related_model._meta
            # The schemas a tenant model's table is in - each tenant's; a related tenant model's
            # table is in the same one.
            schema_names: tuple[str | None, ...] = (
                tenant_schema_names if referencing_meta.tenant_schema and tenant_schema_names else (None,)
            )
            for schema_name in schema_names:
                rows = await connection.execute_dicts(
                    POSTGRESQL_DEFERRABLE_CONSTRAINT_NAME_QUERY,
                    [
                        literals.qualify_table_name(referencing_meta.db_table, schema_name or referencing_meta.schema),
                        literals.qualify_table_name(
                            referenced_meta.db_table,
                            schema_name if referenced_meta.tenant_schema and schema_name else referenced_meta.schema,
                        ),
                        list(foreign_key_field.db_column_names),
                    ],
                )
                names.extend(row["name"] for row in rows if row["name"] not in names)
        constraint_names = tuple(names)
        ProtectConstraintDeferral.CONSTRAINT_NAMES_CACHE[(model, *names_key)] = constraint_names
        return constraint_names

    @staticmethod
    @asynccontextmanager
    async def defer(model: type[Model], connection: DatabaseClient) -> AsyncGenerator[bool]:
        """Defers the PROTECT constraints of a hard delete of ``model`` rows for the block.

        Args:
            model: The model rows are deleted from.
            connection: A client inside the transaction the delete runs in.

        Yields:
            Whether anything was deferred - False when no deferrable PROTECT constraint exists.

        Raises:
            IntegrityError: If a deferred constraint still fails once the block ends.
        """
        names = await PostgresqlForeignKeyDeferral.get_protect_constraint_names(model, connection)
        if not names:
            yield False
            return
        joined_names = ", ".join(names)
        await connection.execute(f"SET CONSTRAINTS {joined_names} DEFERRED")
        try:
            yield True
        except Exception:
            # A failed block takes the deferral back too, where the transaction still runs statements;
            # after a database error it is aborted - rolling back to a savepoint takes the deferral
            # back itself - and the block's own error is the one raised.
            with suppress(HareError):
                await connection.execute(f"SET CONSTRAINTS {joined_names} IMMEDIATE")
            raise
        await connection.execute(f"SET CONSTRAINTS {joined_names} IMMEDIATE")
