from __future__ import annotations

from typing import Any

from hare.sql.sql_context import SqlContext
from hare.sql.terms.functions.function import Function


class TimestampComparand(Function):
    """A timestamp a date column is compared with - the column is compared as ``DateAsTimestamp``.
    Rendered as the timestamp itself."""

    def __init__(self, term: Any, zone_name: str | None, alias: str | None = None) -> None:
        super().__init__("CAST", term, alias=alias)
        self.zone_name = zone_name

    def get_function_sql(self, sql_context: SqlContext) -> str:
        return self.get_arg_sql(self.args[0], sql_context)
