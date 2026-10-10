from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.dialects.clickhouse.functions.aggregates.parametric_aggregate_function import ParametricAggregateFunction
from hare.sql.terms.functions.window_frame_analytic_function import WindowFrameAnalyticFunction

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.sql.sql_context import SqlContext


class ParametricAnalyticFunction(WindowFrameAnalyticFunction):
    """A ClickHouse aggregate with parameters computed over a window -
    ``name(parameters)(arguments) OVER (...)``.

    Args:
        name: The function.
        parameters: Its parameters - numbers, written into the SQL text.
        *args: Its arguments.
    """

    def __init__(self, name: str, parameters: tuple[Any, ...], *args: Any) -> None:
        super().__init__(name, *args)
        self.parameters = parameters

    def get_function_sql(self, sql_context: SqlContext) -> str:
        return ParametricAggregateFunction.get_parametric_sql(self, super().get_function_sql(sql_context), sql_context)
