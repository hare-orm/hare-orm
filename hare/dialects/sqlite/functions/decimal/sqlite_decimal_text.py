from __future__ import annotations

from typing import Any

from hare.dialects.sqlite.functions.constants import SQLITE_DECIMAL_TEXT_FUNCTION_NAME
from hare.sql.sql_context import SqlContext
from hare.sql.terms.functions.function import Function


class SqliteDecimalText(Function):
    """A branch of a Decimal result as its exact decimal text - a ``DecimalField``'s stored text
    unchanged, a number written out - where ``CAST(... AS NUMERIC)`` would turn it into a double. SQL
    kept in DDL names no function: the branch is read as a number there."""

    def __init__(self, term: Any, alias: str | None = None) -> None:
        super().__init__(SQLITE_DECIMAL_TEXT_FUNCTION_NAME, term, alias=alias)

    def get_function_sql(self, sql_context: SqlContext) -> str:
        term_sql = self.get_arg_sql(self.args[0], sql_context)
        if sql_context.native_functions_only:
            return f"CAST({term_sql} AS NUMERIC)"
        return f"{self.name}({term_sql})"
