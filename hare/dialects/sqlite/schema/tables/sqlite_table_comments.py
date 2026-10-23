from __future__ import annotations

from typing import TYPE_CHECKING

from hare.dialects.base.schema.tables.table_comments import TableComments
from hare.dialects.sqlite.schema.constants import SQLITE_COMMENT_ESCAPES

if TYPE_CHECKING:
    from hare.dialects.sqlite.schema.sqlite_schema_editor import SqliteSchemaEditor
    from hare.models import Model


class SqliteTableComments(TableComments):
    """TableComments as SQLite writes it."""

    __slots__ = ()

    editor: SqliteSchemaEditor

    async def alter_table_comment(self, model: type[Model]) -> None:
        """Rebuilds the table - its comment is part of its ``CREATE TABLE`` text.

        Args:
            model: The model, rendered with its new description.
        """
        await self.editor.table_rebuild.remake_table(model, model)

    def get_table_comment_sql(self, table: str, comment: str) -> str:
        return f" /* {comment.translate(SQLITE_COMMENT_ESCAPES)} */"

    def get_column_comment_sql(self, table: str, column: str, comment: str) -> str:
        return f" /* {comment.translate(SQLITE_COMMENT_ESCAPES)} */"
