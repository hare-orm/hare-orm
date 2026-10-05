from __future__ import annotations

from typing import TYPE_CHECKING

from hare.dialects.base.schema.tables.table_creation import TableCreation

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.postgresql.schema.postgresql_schema_editor import PostgresqlSchemaEditor


class PostgresqlTableCreation(TableCreation):
    """TableCreation as PostgreSQL writes it."""

    __slots__ = ()

    editor: PostgresqlSchemaEditor

    def post_table_hook(self) -> str:
        sql = "\n".join(self.editor.comments_array)
        self.editor.comments_array = []
        if sql:
            return "\n" + sql
        return ""
