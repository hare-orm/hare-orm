from __future__ import annotations

from hare.dialects.base.schema.schema_editor_part import SchemaEditorPart
from hare.exceptions import UnSupportedError


class Schemas(SchemaEditorPart):
    """Database schemas: created, dropped, and a table moved from one to another."""

    __slots__ = ()

    def get_schema_create_sql(self, schema: str, safe: bool) -> str:
        """Returns the statement creating a database schema, or an empty string where the
        database has no schemas.

        Args:
            schema: The schema's name.
            safe: Whether it is created only when it doesn't exist yet.

        Returns:
            The statement.
        """
        if not self.editor.client.features.supports_schemas:
            return ""
        return f"CREATE SCHEMA {self.editor.table_creation.get_exists_sql(safe)}{self.editor.quote(schema)};"

    async def create_schema(self, schema_name: str) -> None:
        await self.editor.run_sql(f"CREATE SCHEMA IF NOT EXISTS {self.editor.quote(schema_name)};")

    async def drop_schema(self, schema_name: str) -> None:
        await self.editor.run_sql(f"DROP SCHEMA IF EXISTS {self.editor.quote(schema_name)} CASCADE;")

    async def move_table_to_schema(self, table_name: str, old_schema: str | None, new_schema: str | None) -> None:
        """Moves an existing table to another schema, with its rows, constraints and indexes.

        Args:
            table_name: The table.
            old_schema: The schema it is in; None for the connection's current schema.
            new_schema: The schema it moves to; None for the connection's current schema.

        Raises:
            UnSupportedError: The dialect can't move a table between schemas.
        """
        raise UnSupportedError(f"Moving a table to another schema is not supported on {self.editor.client.dialect}")
