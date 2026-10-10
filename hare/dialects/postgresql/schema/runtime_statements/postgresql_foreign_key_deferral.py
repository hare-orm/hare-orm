from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager, suppress
from typing import TYPE_CHECKING

from hare.dialects.postgresql.schema.constants import POSTGRESQL_DEFERRABLE_CONSTRAINT_NAME_QUERY
from hare.exceptions import HareError
from hare.models.deletion.cascade.deletion_graph import DeletionGraph
from hare.models.deletion.cascade.protect_constraint_deferral import ProtectConstraintDeferral

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
        keys a hard delete of ``model`` rows can run into - read from ``pg_constraint`` and cached
        per model and connection until DDL runs or a relation changes.

        Args:
            model: The model rows are deleted from.
            connection: The connection the delete runs on.

        Returns:
            The names; a constraint that is missing or not deferrable is left out.
        """
        names_key = (connection.connection_alias,)
        cached_names: tuple[str, ...] | None = ProtectConstraintDeferral.CONSTRAINT_NAMES_CACHE.get_for_model(
            model, names_key
        )
        if cached_names is not None:
            return cached_names
        names: list[str] = []
        for foreign_key_field in DeletionGraph.get_cascade_protect_foreign_keys(model):
            referencing_meta = foreign_key_field.model._meta
            referenced_meta = foreign_key_field.related_model._meta
            rows = await connection.execute_dicts(
                POSTGRESQL_DEFERRABLE_CONSTRAINT_NAME_QUERY,
                [
                    connection.dialect.literals.qualify_table_name(referencing_meta.db_table, referencing_meta.schema),
                    connection.dialect.literals.qualify_table_name(referenced_meta.db_table, referenced_meta.schema),
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
