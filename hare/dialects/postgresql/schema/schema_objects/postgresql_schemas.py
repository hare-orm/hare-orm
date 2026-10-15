from __future__ import annotations

from typing import TYPE_CHECKING

from hare.dialects.base.schema.schema_objects.schemas import Schemas
from hare.dialects.postgresql.schema.constants import POSTGRESQL_MOVE_TABLE_TO_CURRENT_SCHEMA_TEMPLATE

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.postgresql.schema.postgresql_schema_editor import PostgresqlSchemaEditor


class PostgresqlSchemas(Schemas):
    """Schemas as PostgreSQL writes it."""

    __slots__ = ()

    editor: PostgresqlSchemaEditor

    async def move_table_to_schema(self, table_name: str, old_schema: str | None, new_schema: str | None) -> None:
        """Moves a table to another schema with ``ALTER TABLE ... SET SCHEMA``, which keeps its
        rows, constraints and indexes.

        Args:
            table_name: The table.
            old_schema: The schema it is in; None for the connection's current schema.
            new_schema: The schema it moves to; None for the connection's current schema.
        """
        if (old_schema or None) == (new_schema or None):
            return
        qualified_table = self.editor.qualify_table_name(table_name, old_schema)
        if new_schema:
            await self.editor.run_sql(f"ALTER TABLE {qualified_table} SET SCHEMA {self.editor.quote(new_schema)};")
            return
        table_literal = "'" + qualified_table.replace("'", "''") + "'"
        await self.editor.run_sql(POSTGRESQL_MOVE_TABLE_TO_CURRENT_SCHEMA_TEMPLATE.format(table_literal=table_literal))
