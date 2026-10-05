from __future__ import annotations

from typing import Any

from hare.dialects.sqlite.functions.constants import SQLITE_DECIMAL_STORED_TEXT_FUNCTION_NAME
from hare.sql.sql_context import SqlContext
from hare.sql.terms.functions.function import Function


class SqliteStoredDecimal(Function):
    """A value set into a ``DECIMAL(max_digits, decimal_places)`` column, as the text a plain write
    stores - the field's scale and digits, and the places of the value's expression, written into
    the SQL text."""

    def __init__(
        self, term: Any, max_digits: int, decimal_places: int, value_scale: int | None, alias: str | None = None
    ) -> None:
        super().__init__(SQLITE_DECIMAL_STORED_TEXT_FUNCTION_NAME, term, alias=alias)
        self.max_digits = int(max_digits)
        self.decimal_places = int(decimal_places)
        self.value_scale = None if value_scale is None else int(value_scale)

    def get_function_sql(self, sql_context: SqlContext) -> str:
        term_sql = self.get_arg_sql(self.args[0], sql_context)
        value_scale_sql = "NULL" if self.value_scale is None else self.value_scale
        return (
            f"{SQLITE_DECIMAL_STORED_TEXT_FUNCTION_NAME}"
            f"({term_sql},{self.max_digits},{self.decimal_places},{value_scale_sql})"
        )
