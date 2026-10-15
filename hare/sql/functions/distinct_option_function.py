from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.sql.builder_methods import BuilderMethods
from hare.sql.sql_context import SqlContext
from hare.sql.terms.functions.aggregate_function import AggregateFunction

if TYPE_CHECKING:
    from typing import Self


class DistinctOptionFunction(AggregateFunction):
    def __init__(self, name: str, *args: Any, **kwargs: Any) -> None:
        alias = kwargs.get("alias")
        super().__init__(name, *args, alias=alias)
        self._distinct = False

    def get_function_sql(self, sql_context: SqlContext) -> str:
        function_sql = super().get_function_sql(sql_context)

        # After the name the SQL was written with - the dialect's own name when it renames it.
        name_length = len(self.get_dialect_special_name(sql_context) or self.name) + 1
        if self._distinct:
            return function_sql[:name_length] + "DISTINCT " + function_sql[name_length:]
        return function_sql

    @BuilderMethods.builder
    def distinct(self) -> Self:
        self._distinct = True
        return self
