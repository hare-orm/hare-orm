from __future__ import annotations

from collections.abc import Sequence
from contextlib import AbstractAsyncContextManager
from typing import TYPE_CHECKING

from hare.dialects.base.client.database_client import DatabaseClient
from hare.dialects.base.schema.runtime_statements.table_clearing import TableClearing
from hare.models import Model

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.sqlite.schema.sqlite_schema_editor import SqliteSchemaEditor


class SqliteTableClearing(TableClearing):
    """TableClearing as SQLite writes it."""

    __slots__ = ()

    editor: SqliteSchemaEditor

    @classmethod
    async def clear_tables(cls, connection: DatabaseClient, quoted_tables: Sequence[str]) -> None:
        # Foreign keys are switched off meanwhile - a self-referencing or circular relation has no
        # delete order that satisfies them.
        await connection.execute_script("PRAGMA foreign_keys = OFF")
        try:
            await super().clear_tables(connection, quoted_tables)
        finally:
            await connection.execute_script("PRAGMA foreign_keys = ON")

    @classmethod
    def defer_cascade_foreign_keys(
        cls, model: type[Model], connection: DatabaseClient
    ) -> AbstractAsyncContextManager[bool]:
        # Local import: the deferral walks hare's models, whose modules import this one.
        from hare.dialects.sqlite.schema.runtime_statements.sqlite_foreign_key_deferral import SqliteForeignKeyDeferral

        return SqliteForeignKeyDeferral.defer(model, connection)
