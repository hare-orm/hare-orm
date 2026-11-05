from __future__ import annotations

from typing import TYPE_CHECKING

from hare.dialects.base.schema.plans.schema_change_plan import SchemaChangePlan
from hare.dialects.base.schema.tables.table_comments import TableComments
from hare.dialects.sqlite.schema.constants import SQLITE_COMMENT_ESCAPES

if TYPE_CHECKING:
    from hare.dialects.sqlite.schema.sqlite_schema_editor import SqliteSchemaEditor
    from hare.models import Model


class SqliteTableComments(TableComments):
    """TableComments as SQLite writes it."""

    __slots__ = ()

    editor: SqliteSchemaEditor

    def get_table_comment_plan(self, old_model: type[Model], new_model: type[Model]) -> SchemaChangePlan:
        """Rebuilds the table - its comment is part of its ``CREATE TABLE`` text.

        Args:
            old_model: The existing table's declaration.
            new_model: The table with its new description.
        """
        return self.editor.table_rebuild.get_rebuild_plan(old_model, new_model)

    def get_table_comment_sql(self, table: str, comment: str) -> str:
        return f" /* {comment.translate(SQLITE_COMMENT_ESCAPES)} */"

    def get_column_comment_sql(self, table: str, column: str, comment: str) -> str:
        return f" /* {comment.translate(SQLITE_COMMENT_ESCAPES)} */"
