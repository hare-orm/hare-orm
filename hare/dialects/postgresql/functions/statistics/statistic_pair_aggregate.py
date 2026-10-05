from __future__ import annotations

from typing import Any, ClassVar

from hare.fields.field import Field
from hare.query.expressions import (
    Aggregate,
    Exists,
    Expression,
    ExpressionResult,
    F,
    Q,
)
from hare.query.expressions.numeric.numeric_typing import NumericTyping
from hare.sql.functions.distinct_option_function import DistinctOptionFunction


class StatisticPairAggregate(Aggregate):
    """A Postgres statistic of two columns per GROUP BY group - ``y`` the dependent one, ``x`` the
    independent one; rows where either is NULL are left out.

    Args:
        y: The dependent field name or expression.
        x: The independent field name or expression.
        distinct: Only distinct ``(y, x)`` pairs.
        _filter: Only the rows matching this condition.
    """

    #: The Postgres aggregate.
    function_name: ClassVar[str] = ""

    def __init__(
        self,
        y: str | F | Expression,
        x: str | F | Expression,
        *,
        distinct: bool = False,
        _filter: Q | Exists | None = None,
    ) -> None:
        super().__init__(y, F(x) if isinstance(x, str) else x, distinct=distinct, _filter=_filter)  # type: ignore[arg-type]

    def get_function_name(self) -> str:
        """The Postgres aggregate this call computes."""
        return self.function_name

    def _get_function_field(self, field: Any, *default_values: Any) -> DistinctOptionFunction:
        function = DistinctOptionFunction(self.get_function_name(), field, *default_values)
        if self.distinct:
            function = function.distinct()
        return function

    def _get_output_field(
        self, function_arg: ExpressionResult, default_results: list[ExpressionResult]
    ) -> Field[Any] | None:
        return NumericTyping.FLOAT_OUTPUT_FIELD  # type: ignore[arg-type]
