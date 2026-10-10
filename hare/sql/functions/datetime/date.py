from __future__ import annotations

from typing import Any

from hare.sql.sql_context import SqlContext
from hare.sql.terms.functions.function import Function


class Date(Function):
    """The date of a term - ISO SQL's ``CAST(term AS DATE)``."""

    def __init__(self, term: Any, alias: str | None = None) -> None:
        super().__init__("DATE", term, alias=alias)

    def get_function_sql(self, sql_context: SqlContext) -> str:
        return f"CAST({self.get_arg_sql(self.args[0], sql_context)} AS DATE)"
