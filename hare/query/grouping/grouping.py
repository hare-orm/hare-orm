from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

from hare.exceptions import QueryError, UnSupportedError
from hare.query.expressions.expression import Expression
from hare.query.expressions.expression_result import ExpressionResult
from hare.query.expressions.f import F
from hare.query.expressions.numeric.numeric_typing import NumericTyping
from hare.query.plans.description.declared_plan_parts import DeclaredPlanParts
from hare.query.plans.enums import PlanPartType
from hare.sql.functions.grouping_function import GroupingFunction

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.query.expressions.expression_context import ExpressionContext


class Grouping(Expression):
    """``GROUPING(field, ...)`` - in a query grouped by ``Rollup``/``Cube``/``GroupingSets``, which
    fields a row's group leaves out: an int with a bit per field, the last field the lowest, set
    where the group doesn't group by it - ``Grouping("city")`` is 1 in the region subtotals, telling
    their NULL city from a NULL value. Computed per group, like an aggregate.

    Args:
        fields: Fields the query groups by.

    Raises:
        QueryError: No field name is given.
    """

    populate_field_object = True
    value_field = NumericTyping.INTEGER_OUTPUT_FIELD  # type: ignore[arg-type]

    plan_parts: ClassVar[DeclaredPlanParts] = (("fields", PlanPartType.KEY),)

    def __init__(self, *fields: str) -> None:
        if not fields or not all(isinstance(field, str) and field for field in fields):
            raise QueryError(f"Grouping() takes the grouped field names, got {fields!r}")
        self.fields = fields

    def get_result(self, expression_context: ExpressionContext) -> ExpressionResult:
        features = (
            expression_context.connection.features
            if expression_context.connection is not None
            else expression_context.dialect.features
        )
        if not features.supports_grouping_sets:
            raise UnSupportedError(f"Grouping() needs GROUPING(), which {expression_context.dialect} doesn't have")
        terms: list[Any] = []
        joins: list[Any] = []
        for field_name in self.fields:
            result = F(field_name).get_result(expression_context)
            terms.append(result.term)
            if result.joins:
                joins = ExpressionResult.dedup_joins(joins, result.joins)
        return ExpressionResult(
            term=GroupingFunction(*terms),
            joins=joins,
            output_field=NumericTyping.INTEGER_OUTPUT_FIELD,  # type: ignore[arg-type]
        )
