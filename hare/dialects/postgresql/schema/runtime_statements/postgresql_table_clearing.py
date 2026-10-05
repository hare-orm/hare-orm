from __future__ import annotations

from collections.abc import Sequence
from contextlib import AbstractAsyncContextManager
from typing import TYPE_CHECKING

from hare.dialects.base.client.database_client import DatabaseClient
from hare.dialects.base.schema.runtime_statements.table_clearing import TableClearing
from hare.models import Model

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.postgresql.schema.postgresql_schema_editor import PostgresqlSchemaEditor


class PostgresqlTableClearing(TableClearing):
    """TableClearing as PostgreSQL writes it."""

    __slots__ = ()

    editor: PostgresqlSchemaEditor

    @classmethod
    async def clear_tables(cls, connection: DatabaseClient, quoted_tables: Sequence[str]) -> None:
        # Local import: the constants module instantiates this class.
        from hare.dialects.postgresql.schema.constants import POSTGRESQL_CLEAR_TABLES_PROBE_SIZE

        # Only the tables holding rows are truncated - TRUNCATE costs the server per table, an empty
        # one as much as a filled one, and a test usually leaves rows in a few.
        filled_tables: list[str] = []
        for start in range(0, len(quoted_tables), POSTGRESQL_CLEAR_TABLES_PROBE_SIZE):
            probed_tables = quoted_tables[start : start + POSTGRESQL_CLEAR_TABLES_PROBE_SIZE]
            probe_sql = " UNION ALL ".join(
                f"SELECT {table_index} AS table_index WHERE EXISTS (SELECT 1 FROM {quoted_table})"  # nosec B608
                for table_index, quoted_table in enumerate(probed_tables)
            )
            filled_tables.extend(
                probed_tables[row["table_index"]] for row in await connection.execute_dicts(probe_sql)
            )
        # One TRUNCATE ... CASCADE empties them all at once, whatever references what.
        if filled_tables:
            await connection.execute_script(f"TRUNCATE {', '.join(filled_tables)} CASCADE")

    @classmethod
    def defer_cascade_foreign_keys(
        cls, model: type[Model], connection: DatabaseClient
    ) -> AbstractAsyncContextManager[bool]:
        # Local import: the deferral walks hare's models, whose modules import this one.
        from hare.dialects.postgresql.schema.runtime_statements.postgresql_foreign_key_deferral import (
            PostgresqlForeignKeyDeferral,
        )

        return PostgresqlForeignKeyDeferral.defer(model, connection)
