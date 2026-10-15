from __future__ import annotations

from hare.sql.sql_context import SqlContext
from hare.sql.terms.functions.function import Function


class Now(Function):
    """The current moment - ISO SQL's ``CURRENT_TIMESTAMP``."""

    def __init__(self, alias: str | None = None) -> None:
        super().__init__("CURRENT_TIMESTAMP", alias=alias)

    def get_function_sql(self, sql_context: SqlContext) -> str:
        return "CURRENT_TIMESTAMP"
