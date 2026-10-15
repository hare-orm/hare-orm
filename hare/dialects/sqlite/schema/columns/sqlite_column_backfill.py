from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.dialects.base.schema.columns.column_backfill import ColumnBackfill
from hare.dialects.sqlite.schema.constants import SQLITE_ROW_IDENTITY_COLUMN

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.sqlite.schema.sqlite_schema_editor import SqliteSchemaEditor
    from hare.fields.field import Field
    from hare.models.model import Model


class SqliteColumnBackfill(ColumnBackfill):
    """ColumnBackfill as SQLite writes it."""

    __slots__ = ()

    editor: SqliteSchemaEditor

    def get_row_identity_sql(self) -> str | None:
        # A table without a primary key is never WITHOUT ROWID - SQLite refuses that.
        return SQLITE_ROW_IDENTITY_COLUMN

    async def set_added_column_not_null(self, model: type[Model], field: Field[Any], db_field: str) -> None:
        # No ALTER COLUMN ... SET NOT NULL on SQLite - the rebuild takes the column's NOT NULL
        # (and every other property) from the model's current metadata.
        await self.editor.table_rebuild.remake_table(model, added_column_name=db_field)
