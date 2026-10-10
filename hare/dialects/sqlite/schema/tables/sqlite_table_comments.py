from __future__ import annotations

from typing import TYPE_CHECKING

from hare.dialects.base.schema.tables.table_comments import TableComments
from hare.dialects.sqlite.schema.constants import SQLITE_COMMENT_ESCAPES

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.sqlite.schema.sqlite_schema_editor import SqliteSchemaEditor


class SqliteTableComments(TableComments):
    """TableComments as SQLite writes it."""

    __slots__ = ()

    editor: SqliteSchemaEditor

    def get_table_comment_sql(self, table: str, comment: str) -> str:
        return f" /* {comment.translate(SQLITE_COMMENT_ESCAPES)} */"

    def get_column_comment_sql(self, table: str, column: str, comment: str) -> str:
        return f" /* {comment.translate(SQLITE_COMMENT_ESCAPES)} */"
