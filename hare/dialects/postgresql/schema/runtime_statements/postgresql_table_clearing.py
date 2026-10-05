from __future__ import annotations

from collections.abc import Sequence
from contextlib import AbstractAsyncContextManager
from typing import TYPE_CHECKING

from hare.dialects.base.client.database_client import DatabaseClient
from hare.dialects.base.schema.runtime_statements.table_clearing import TableClearing
from hare.dialects.postgresql.schema.constants import (
    POSTGRESQL_CLEAR_TABLES_PROBE_MAX_LENGTH,
    POSTGRESQL_CLEAR_TABLES_PROBE_PART_TEMPLATE,
    POSTGRESQL_CLEAR_TABLES_PROBE_SEPARATOR,
)
from hare.models import Model

if TYPE_CHECKING:
    from hare.dialects.postgresql.schema.postgresql_schema_editor import PostgresqlSchemaEditor


class PostgresqlTableClearing(TableClearing):
    """TableClearing as PostgreSQL writes it."""

    __slots__ = ()

    editor: PostgresqlSchemaEditor

    @classmethod
    async def clear_tables(cls, connection: DatabaseClient, quoted_tables: Sequence[str]) -> None:
        # Only the tables holding rows are truncated - TRUNCATE costs the server per table, an empty
        # one as much as a filled one, and a test usually leaves rows in a few.
        filled_tables: list[str] = []
        for probed_tables in cls.get_probed_table_groups(quoted_tables):
            probe_sql = POSTGRESQL_CLEAR_TABLES_PROBE_SEPARATOR.join(
                POSTGRESQL_CLEAR_TABLES_PROBE_PART_TEMPLATE.format(table_index=table_index, table=quoted_table)
                for table_index, quoted_table in enumerate(probed_tables)
            )
            filled_tables.extend(
                probed_tables[row["table_index"]] for row in await connection.execute_dicts(probe_sql)
            )
        # One TRUNCATE ... CASCADE empties them all at once, whatever references what.
        if filled_tables:
            await connection.execute_script(f"TRUNCATE {', '.join(filled_tables)} CASCADE")

    @staticmethod
    def get_probed_table_groups(quoted_tables: Sequence[str]) -> list[list[str]]:
        """The tables in groups whose "do you hold a row" query stays within
        ``POSTGRESQL_CLEAR_TABLES_PROBE_MAX_LENGTH``.

        Args:
            quoted_tables: The tables, quoted.

        Returns:
            The groups, in order.
        """
        groups: list[list[str]] = []
        group: list[str] = []
        group_length = 0
        for quoted_table in quoted_tables:
            part_length = len(POSTGRESQL_CLEAR_TABLES_PROBE_SEPARATOR) + len(
                POSTGRESQL_CLEAR_TABLES_PROBE_PART_TEMPLATE.format(table_index=len(group), table=quoted_table)
            )
            if group and group_length + part_length > POSTGRESQL_CLEAR_TABLES_PROBE_MAX_LENGTH:
                groups.append(group)
                group, group_length = [], 0
            group.append(quoted_table)
            group_length += part_length
        if group:
            groups.append(group)
        return groups

    @classmethod
    def defer_cascade_foreign_keys(
        cls, model: type[Model], connection: DatabaseClient
    ) -> AbstractAsyncContextManager[bool]:
        # Local import: the deferral walks hare's models, whose modules import this one.
        from hare.dialects.postgresql.schema.runtime_statements.postgresql_foreign_key_deferral import (
            PostgresqlForeignKeyDeferral,
        )

        return PostgresqlForeignKeyDeferral.defer(model, connection)
