from __future__ import annotations

from typing import TYPE_CHECKING

from hare.dialects.base.schema.schema_objects.schemas import Schemas
from hare.dialects.postgresql.schema.constants import POSTGRESQL_MOVE_OBJECT_TO_CURRENT_SCHEMA_TEMPLATE

if TYPE_CHECKING:
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
        await self.move_object_to_schema("TABLE", self.editor.qualify_table_name(table_name, old_schema), new_schema)

    async def move_object_to_schema(self, object_type: str, qualified_name: str, new_schema: str | None) -> None:
        """Moves an object to another schema - ``ALTER <type> <name> SET SCHEMA``.

        Args:
            object_type: The object's type in SQL: ``TABLE``, ``VIEW``, ``MATERIALIZED VIEW``,
                ``SEQUENCE``, ``FUNCTION``.
            qualified_name: Its name in its schema - a function's with its argument types.
            new_schema: The schema it moves to; None for the connection's current schema.
        """
        if new_schema:
            await self.editor.run_sql(
                f"ALTER {object_type} {qualified_name} SET SCHEMA {self.editor.quote(new_schema)};"
            )
            return
        name_literal = "'" + qualified_name.replace("'", "''") + "'"
        await self.editor.run_sql(
            POSTGRESQL_MOVE_OBJECT_TO_CURRENT_SCHEMA_TEMPLATE.format(
                object_type=object_type, name_literal=name_literal
            )
        )
