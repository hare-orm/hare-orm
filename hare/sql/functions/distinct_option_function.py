from __future__ import annotations

from typing import TYPE_CHECKING

from hare.sql.context import SqlContext
from hare.sql.terms.functions.aggregate_function import AggregateFunction
from hare.sql.utils import builder

if TYPE_CHECKING:
    from typing import Self


class DistinctOptionFunction(AggregateFunction):
    def __init__(self, name: str, *args, **kwargs) -> None:
        alias = kwargs.get("alias")
        super().__init__(name, *args, alias=alias)
        self._distinct = False

    def get_function_sql(self, ctx: SqlContext) -> str:
        s = super().get_function_sql(ctx)

        n = len(self.name) + 1
        if self._distinct:
            return s[:n] + "DISTINCT " + s[n:]
        return s

    @builder
    def distinct(self) -> Self:  # type:ignore[return]
        self._distinct = True
