from __future__ import annotations

from typing import Any, ClassVar, cast

from hare.fields.enums import ValueComputation
from hare.fields.field import Field
from hare.query.expressions import Aggregate
from hare.query.expressions.expression_context import ExpressionContext
from hare.query.expressions.expression_result import ExpressionResult
from hare.query.expressions.numeric.numeric_typing import NumericTyping


class StatisticResultAggregate(Aggregate):
    """An aggregate whose result is a statistic of the values - ``Avg`` and the population and sample
    statistics: of integers or Decimals an exact Decimal where the database computes one
    (``Features.computes_integer_statistics_exactly``, ``computes_decimal_statistics_exactly``), else a
    float."""

    populate_field_object = True
    #: What it computes - the database may compute an average in a wider type than another statistic.
    computation: ClassVar[ValueComputation] = ValueComputation.STATISTIC

    def _finalize_result(
        self, result: ExpressionResult, function_arg: ExpressionResult, expression_context: ExpressionContext
    ) -> ExpressionResult:
        """Adapt the statistic after the aggregate's keyed term and NULL metadata are complete."""
        result = super()._finalize_result(result, function_arg, expression_context)
        if not NumericTyping.is_statistic_a_float(function_arg.output_field, expression_context.dialect):
            return result
        float_term = expression_context.dialect.renderers.get_statistic_as_float(result.term)
        if float_term is result.term:
            return result
        return ExpressionResult(term=float_term, joins=result.joins, output_field=result.output_field)

    def _coerce_output_field(self, field_object: Field[Any], expression_context: ExpressionContext) -> Field[Any]:
        return cast(
            "Field[Any]",
            NumericTyping.get_statistic_output_field(field_object, expression_context.dialect, self.computation),
        )
