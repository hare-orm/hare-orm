from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.sql.context import SqlContext
from hare.sql.terms.functions.function import Function

if TYPE_CHECKING:
    pass


class Cast(Function):
    def __init__(self, term: Any, as_type: Any, alias: str | None = None) -> None:
        super().__init__("CAST", term, alias=alias)
        self.as_type = as_type

    def get_special_params_sql(self, ctx: SqlContext) -> str:
        type_sql = self.as_type.get_sql(ctx) if hasattr(self.as_type, "get_sql") else str(self.as_type).upper()

        return f"AS {type_sql}"
