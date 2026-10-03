from __future__ import annotations

from typing import Any

from hare.dialects.sqlite.constants import SQLITE_DECIMAL_STORED_TEXT_FUNCTION_NAME
from hare.sql.context import SqlContext
from hare.sql.terms.functions.function import Function


class SqliteStoredDecimal(Function):
    """A value set into a ``DECIMAL(max_digits, decimal_places)`` column, as the text a plain write
    stores - the field's scale and digits written into the SQL text."""

    def __init__(self, term: Any, max_digits: int, decimal_places: int, alias: str | None = None) -> None:
        super().__init__(SQLITE_DECIMAL_STORED_TEXT_FUNCTION_NAME, term, alias=alias)
        self.max_digits = int(max_digits)
        self.decimal_places = int(decimal_places)

    def get_function_sql(self, ctx: SqlContext) -> str:
        term_sql = self.get_arg_sql(self.args[0], ctx)
        return f"{SQLITE_DECIMAL_STORED_TEXT_FUNCTION_NAME}({term_sql},{self.max_digits},{self.decimal_places})"
