from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from hare.dialects.clickhouse.functions.aggregates.parametric_aggregate_function import ParametricAggregateFunction
from hare.dialects.clickhouse.functions.aggregates.parametric_analytic_function import ParametricAnalyticFunction
from hare.query.expressions import Aggregate

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.sql.terms.functions import AggregateFunction
    from hare.sql.terms.functions.analytic_function import AnalyticFunction


class ParametricAggregate(Aggregate, abstract=True):
    """Base of the ClickHouse aggregates taking parameters before their arguments -
    ``name(parameters)(arguments)``. A subclass names the function and gives the parameters - each
    attribute they come from a key of its plan."""

    computed_over_window = True

    def get_parameters(self) -> tuple[Any, ...]:
        """The parameters - numbers, written into the SQL text.

        Returns:
            The parameters, none by default.
        """
        return ()

    def get_function_name(self) -> str:
        """The ClickHouse function.

        Returns:
            ``function_name`` by default.
        """
        return str(self.function_name)

    def _get_function_field(self, field: Any, *default_values: Any) -> ParametricAggregateFunction:
        function = ParametricAggregateFunction(self.get_function_name(), self.get_parameters(), field, *default_values)
        return function.distinct() if self.distinct else function

    def get_analytic_function(self, function: AggregateFunction) -> AnalyticFunction:
        parametric_function = cast("ParametricAggregateFunction", function)
        analytic_function = ParametricAnalyticFunction(
            parametric_function.name, parametric_function.parameters, *parametric_function.args
        )
        if parametric_function._include_filter:
            analytic_function = analytic_function.filter(*function._filters)
        return analytic_function
