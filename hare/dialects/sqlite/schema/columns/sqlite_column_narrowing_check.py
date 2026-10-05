from __future__ import annotations

from typing import TYPE_CHECKING

from hare.dialects.base.schema.columns.column_narrowing_check import ColumnNarrowingCheck
from hare.dialects.sqlite.constants import (
    SQLITE_DECIMAL_OVERFLOWS_FUNCTION_NAME,
)

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.sqlite.schema.sqlite_schema_editor import SqliteSchemaEditor


class SqliteColumnNarrowingCheck(ColumnNarrowingCheck):
    """ColumnNarrowingCheck as SQLite writes it."""

    __slots__ = ()

    editor: SqliteSchemaEditor

    def get_decimal_overflow_predicate_sql(self, quoted_column: str, max_digits: int, decimal_places: int) -> str:
        # Decimals are stored as text and CAST(... AS NUMERIC) gives a double - the value is read
        # as its exact decimal text instead.
        return f"{SQLITE_DECIMAL_OVERFLOWS_FUNCTION_NAME}({quoted_column}, {max_digits}, {decimal_places})"
