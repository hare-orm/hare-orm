from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from hare.exceptions import IntegrityError
from hare.models.deletion.deletion_graph import DeletionGraph

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.models import Model


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
    async def check_cascade_foreign_keys(model: type[Model], db: DatabaseClient) -> None:
        """Checks every foreign key constraint pointing at a model of ``model``'s cascade - relation
        fields and automatic many-to-many through tables - which a hard delete can leave failing
        while ``PRAGMA defer_foreign_keys`` is on.

        Args:
            model: The model rows are deleted from.
            db: A client inside the delete's transaction.

        Raises:
            IntegrityError: A row still points at a row the delete removed.
        """
        cascade_models = DeletionGraph.get_cascade_models(model)
        cascade_tables = {cascade_model._meta.db_table for cascade_model in cascade_models}
        referencing_tables: dict[tuple[str | None, str], None] = {}
        for cascade_model in cascade_models:
            for __, fk_field in DeletionGraph.get_backward_relations(cascade_model):
                if fk_field.has_database_constraint:
                    referencing_tables[(fk_field.model._meta.schema, fk_field.model._meta.db_table)] = None
            for m2m_field in DeletionGraph.get_m2m_fields(cascade_model):
                if m2m_field.through_model is None and m2m_field.has_database_constraint:
                    referencing_tables[(cascade_model._meta.schema, m2m_field.through)] = None
        for schema, table in referencing_tables:
            pragma = f"{db.dialect.quote_identifier(schema)}.foreign_key_check" if schema else "foreign_key_check"
            violations = await db.execute_dicts(f"PRAGMA {pragma}({db.dialect.quote_identifier(table)})")
            blocking_parents = sorted({row["parent"] for row in violations if row["parent"] in cascade_tables})
            if blocking_parents:
                raise IntegrityError(
                    f"FOREIGN KEY constraint failed: {table} rows still point at deleted "
                    f"{', '.join(blocking_parents)} rows"
                )

    @staticmethod
    @asynccontextmanager
    async def defer(model: type[Model], db: DatabaseClient) -> AsyncGenerator[bool]:
        """Defers every foreign key of the connection for the block, checking the ones pointing
        into the cascade of ``model`` before the deferral ends.

        Args:
            model: The model rows are deleted from.
            db: A client inside the transaction the delete runs in.

        Yields:
            Whether anything was deferred - False when ``PRAGMA defer_foreign_keys`` was already on.

        Raises:
            IntegrityError: If a foreign key pointing into the cascade still fails once the block ends.
        """
        (defer_row,) = await db.execute_dicts("PRAGMA defer_foreign_keys")
        if next(iter(defer_row.values())):
            yield False
            return
        await db.execute("PRAGMA defer_foreign_keys = ON")
        try:
            yield True
            await SqliteForeignKeyDeferral.check_cascade_foreign_keys(model, db)
        finally:
            await db.execute("PRAGMA defer_foreign_keys = OFF")
