from __future__ import annotations

from typing import Any, ClassVar

from hare.dialects.postgresql.lookups.trigram_operator_term import TrigramOperatorTerm
from hare.fields.data.numeric.float_field import FloatField
from hare.query.expressions import Expression, ExpressionContext, ExpressionResult, F, Value
from hare.query.plans.description.plan_context import PlanContext
from hare.query.plans.description.plan_description import PlanDescription
from hare.sql.terms.functions.function import Function


class TrigramFunction(Expression):
    """A pg_trgm similarity or distance of a text expression and a string, a float from 0 to 1.

    Args:
        expression: The field name or text expression.
        string: The string compared with it.
    """

    #: Shared, long-lived instance - the statement plans hold output fields weakly.
    OUTPUT_FIELD: ClassVar[FloatField[Any]] = FloatField()

    #: The pg_trgm function, or the distance operator.
    function_name: ClassVar[str] = ""
    #: Whether ``function_name`` is an operator (``<->``), not a function.
    is_operator: ClassVar[bool] = False
    #: Whether the string is the first argument (the word similarities), not the second.
    string_first: ClassVar[bool] = False

    populate_field_object = True

    def __init__(self, expression: str | Expression, string: str) -> None:
        self.expression = F(expression) if isinstance(expression, str) else expression
        self.string = string

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.expression!r}, {self.string!r})"

    def get_plan_description(self, context: PlanContext) -> PlanDescription | None:
        """The text expression's description, then the compared string, bound.

        Args:
            context: The context the expression is resolved in.

        Returns:
            The description, None when the text expression keeps no plan.
        """
        return PlanDescription.combine(
            type(self),
            (self.expression.get_plan_description(context), Value(self.string).get_plan_description(context)),
        )

    def get_result(self, expression_context: ExpressionContext) -> ExpressionResult:
        expression_result = self.expression.get_result(expression_context)
        string_term = Value(self.string).get_result(expression_context).term
        left, right = (
            (string_term, expression_result.term) if self.string_first else (expression_result.term, string_term)
        )
        term = (
            TrigramOperatorTerm(self.function_name, left, right)
            if self.is_operator
            else Function(self.function_name, left, right)
        )
        return ExpressionResult(term=term, joins=expression_result.joins, output_field=self.OUTPUT_FIELD)  # type: ignore[call-overload]

    value_field = OUTPUT_FIELD
