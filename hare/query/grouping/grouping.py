from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

from hare.exceptions import QueryError, UnSupportedError
from hare.query.expressions.expression import Expression
from hare.query.expressions.expression_result import ExpressionResult
from hare.query.expressions.numeric.numeric_typing import NumericTyping
from hare.query.expressions.structure.enums import ArgumentRole
from hare.query.expressions.structure.expression_structure import ExpressionStructure
from hare.query.grouping.grouping_expressions import GroupingExpressions
from hare.query.plans.description.declared_plan_parts import DeclaredPlanParts
from hare.query.plans.enums import PlanPartType
from hare.sql.functions.grouping_function import GroupingFunction
from hare.sql.terms.case.case import Case

if TYPE_CHECKING:
    from hare.query.expressions.expression_context import ExpressionContext
    from hare.sql.terms.term import Term


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

    structure = ExpressionStructure(positional_role=ArgumentRole.FIELD)

    populate_field_object = True
    value_field = NumericTyping.INTEGER_OUTPUT_FIELD

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
        if expression_context.checks_dialect_admission and not features.supports_grouping_sets:
            raise UnSupportedError(f"Grouping() needs GROUPING(), which {expression_context.dialect} doesn't have")
        terms_by_field: list[list[Any]] = []
        joins: list[Any] = []
        for field_name in self.fields:
            results = GroupingExpressions.get_results(expression_context, field_name)
            terms_by_field.append([result.term for result in results])
            for result in results:
                joins = ExpressionResult.dedup_joins(joins, result.joins)
        term: Term
        if all(len(terms) == 1 for terms in terms_by_field):
            term = GroupingFunction(*(terms[0] for terms in terms_by_field))
        else:
            # A composite logical field has one bit, not a bit for each stored column.
            bits = [
                GroupingFunction(*terms) if len(terms) == 1 else Case().when(GroupingFunction(*terms) == 0, 0).else_(1)
                for terms in terms_by_field
            ]
            term = bits[0]
            for bit in bits[1:]:
                term = term * 2 + bit
        return ExpressionResult(
            term=term,
            joins=joins,
            output_field=NumericTyping.INTEGER_OUTPUT_FIELD,
        )
