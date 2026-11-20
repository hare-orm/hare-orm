from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from hare.core.connections.relation_constraints import RelationConstraints
from hare.dialects.sqlite.client.sqlite_pragma_scope import SqlitePragmaScope
from hare.exceptions import IntegrityError
from hare.models.deletion.cascade.deletion_graph import DeletionGraph

if TYPE_CHECKING:
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.models.model_binding import ModelBinding


class SqliteForeignKeyDeferral:
    """Defers the foreign keys a SQLite cascade delete can run into.

    SQLite checks a ``NO ACTION`` constraint once, at the end of the whole statement, so a single
    ``DELETE`` needs no deferral; a delete made of several statements (a per-row fallback, a
    Python-side cascade step) does. ``PRAGMA defer_foreign_keys`` defers every foreign key of the
    connection, and switching it off forgets their pending violations - so before that, every
    constraint pointing into the cascade is checked with ``PRAGMA foreign_key_check``. A pragma
    that was already on is left alone.
    """

    @staticmethod
    async def check_cascade_foreign_keys(binding: ModelBinding, connection: DatabaseClient) -> None:
        """Checks every foreign key constraint pointing at a model of ``model``'s cascade - relation
        fields and automatic many-to-many through tables - which a hard delete can leave failing
        while ``PRAGMA defer_foreign_keys`` is on.

        Args:
            binding: The model rows are deleted from.
            connection: A client inside the delete's transaction.

        Raises:
            IntegrityError: A row still points at a row the delete removed.
        """
        from hare.core.hare_context import HareContext

        cascade_bindings = [binding.graph.get_binding(model) for model in DeletionGraph.get_cascade_models(binding)]
        cascade_tables = {cascade_binding.db_table for cascade_binding in cascade_bindings}
        referencing_tables: dict[tuple[str | None, str], None] = {}
        for cascade_binding in cascade_bindings:
            for __, foreign_key_field in DeletionGraph.get_backward_relations(cascade_binding):
                if RelationConstraints.is_enforced(
                    foreign_key_field, HareContext.require_current(), connection=connection, source_binding=binding
                ):
                    referencing_binding = foreign_key_field.get_binding().model_binding
                    referencing_tables[
                        (
                            referencing_binding.schema,  # type: ignore[union-attr]
                            referencing_binding.db_table,  # type: ignore[union-attr]
                        )
                    ] = None
            for many_to_many_field in DeletionGraph.get_many_to_many_fields(cascade_binding):
                if many_to_many_field.through_model is None and RelationConstraints.is_enforced(
                    many_to_many_field, HareContext.require_current(), connection=connection, source_binding=binding
                ):
                    referencing_tables[(cascade_binding.schema, many_to_many_field.through)] = None
        quote_identifier = connection.dialect.literals.quote_identifier
        for schema, table in referencing_tables:
            pragma = f"{quote_identifier(schema)}.foreign_key_check" if schema else "foreign_key_check"
            violations = await connection.execute_dicts(f"PRAGMA {pragma}({quote_identifier(table)})")
            blocking_parents = sorted({row["parent"] for row in violations if row["parent"] in cascade_tables})
            if blocking_parents:
                raise IntegrityError(
                    f"FOREIGN KEY constraint failed: {table} rows still point at deleted "
                    f"{', '.join(blocking_parents)} rows"
                )

    @staticmethod
    @asynccontextmanager
    async def defer(binding: ModelBinding, connection: DatabaseClient) -> AsyncGenerator[bool]:
        """Defers every foreign key of the connection for the block, checking the ones pointing
        into the cascade of ``model`` before the deferral ends.

        Args:
            binding: The model rows are deleted from.
            connection: A client inside the transaction the delete runs in.

        Yields:
            Whether anything was deferred - False when ``PRAGMA defer_foreign_keys`` was already on.

        Raises:
            IntegrityError: If a foreign key pointing into the cascade still fails once the block ends.
        """
        (defer_row,) = await connection.execute_dicts("PRAGMA defer_foreign_keys")
        if next(iter(defer_row.values())):
            yield False
            return
        async with SqlitePragmaScope.run(connection, "defer_foreign_keys", "ON", previous="OFF"):
            yield True
            await SqliteForeignKeyDeferral.check_cascade_foreign_keys(binding, connection)
