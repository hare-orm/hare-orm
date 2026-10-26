from __future__ import annotations

from typing import TYPE_CHECKING

from hare.dialects.base.schema.columns.column_narrowing_check import ColumnNarrowingCheck

if TYPE_CHECKING:
    from hare.dialects.sqlite.schema.sqlite_schema_editor import SqliteSchemaEditor


class SqliteColumnNarrowingCheck(ColumnNarrowingCheck):
    """ColumnNarrowingCheck as SQLite writes it."""

    __slots__ = ()

    editor: SqliteSchemaEditor

    def get_time_of_day_predicate_sql(self, quoted_column: str) -> str:
        # A moment is its ISO text - its clock and the microseconds after its seconds.
        return (
            f"time({quoted_column}) <> '00:00:00' "
            f"OR (substr({quoted_column}, 20, 1) = '.' AND CAST(substr({quoted_column}, 21, 6) AS INTEGER) <> 0)"
        )
