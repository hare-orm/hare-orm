from __future__ import annotations

from typing import Any

from hare.dialects.sqlite.constants import SQLITE_DECIMAL_COLLATION_NAME
from hare.dialects.sqlite.functions.constants import SQLITE_DECIMAL_TEXT_FUNCTION_NAME
from hare.sql import functions
from hare.sql.sql_context import SqlContext
from hare.sql.terms.functions.function import Function


class SqliteDecimalComparand(Function):
    """A value compared with Decimals as its exact decimal text, under the decimal collation -
    `CAST(... AS NUMERIC)` would turn both sides into doubles."""

    def __init__(self, term: Any, alias: str | None = None) -> None:
        super().__init__(SQLITE_DECIMAL_TEXT_FUNCTION_NAME, functions.Collate.strip(term), alias=alias)

    def get_function_sql(self, sql_context: SqlContext) -> str:
        term_sql = self.get_arg_sql(self.args[0], sql_context)
        return f"({SQLITE_DECIMAL_TEXT_FUNCTION_NAME}({term_sql}) COLLATE {SQLITE_DECIMAL_COLLATION_NAME})"
