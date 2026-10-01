from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.sql.context import SqlContext
from hare.sql.terms.functions.function import Function

if TYPE_CHECKING:
    pass


class TimestampComparand(Function):
    """A timestamp a date column is compared with - the column is compared as ``DateAsTimestamp``.
    Rendered as the timestamp itself."""

    def __init__(self, term: Any, zone_name: str | None, alias: str | None = None) -> None:
        super().__init__("CAST", term, alias=alias)
        self.zone_name = zone_name

    def get_function_sql(self, ctx: SqlContext) -> str:
        return self.get_arg_sql(self.args[0], ctx)
