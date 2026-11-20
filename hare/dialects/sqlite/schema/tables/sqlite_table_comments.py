from __future__ import annotations

from typing import TYPE_CHECKING

from hare.dialects.base.schema.plans.schema_change_plan import SchemaChangePlan
from hare.dialects.base.schema.tables.table_comments import TableComments
from hare.dialects.sqlite.schema.constants import SQLITE_COMMENT_ESCAPES

if TYPE_CHECKING:
    from hare.dialects.sqlite.schema.sqlite_schema_editor import SqliteSchemaEditor
    from hare.models.model_binding import ModelBinding


class SqliteTableComments(TableComments):
    """TableComments as SQLite writes it."""

    __slots__ = ()

    editor: SqliteSchemaEditor

    def get_table_comment_plan(self, old_binding: ModelBinding, new_binding: ModelBinding) -> SchemaChangePlan:
        """Rebuilds the table - its comment is part of its ``CREATE TABLE`` text.

        Args:
            old_binding: The binding of the existing table's declaration.
            new_binding: The binding of the table with its new description.
        """
        return self.editor.table_rebuild.get_rebuild_plan(old_binding, new_binding)

    def get_table_comment_sql(self, table: str, comment: str) -> str:
        return f" /* {comment.translate(SQLITE_COMMENT_ESCAPES)} */"

    def get_column_comment_sql(self, table: str, column: str, comment: str) -> str:
        return f" /* {comment.translate(SQLITE_COMMENT_ESCAPES)} */"
