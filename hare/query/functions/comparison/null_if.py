from typing import Any

from hare.query.expressions import Expression, Function
from hare.query.expressions.base.expression_context import ExpressionContext
from hare.query.expressions.base.expression_result import ExpressionResult
from hare.query.expressions.base.value import Value
from hare.query.functions.comparison.function_arguments import FunctionArguments
from hare.sql.terms.base.term import Term
from hare.sql.terms.functions.function import Function as SqlFunction


class NullIf(Function):
    """NULL when the value equals ``other``, the value otherwise: ``NullIf("discount", 0)``. A string
    ``other`` is text - pass a field as ``F("other")``."""

    populate_field_object = True

    def __init__(self, expression: Any, other: Any) -> None:
        """
        Args:
            expression: A field name, an expression or a literal.
            other: An expression or a literal.
        """
        other_expression = other if isinstance(other, (Expression, Term)) else Value(other)
        super().__init__(FunctionArguments.get_expression(expression), other_expression)

    def _get_function_field(self, field: Term | str, *default_values: Any) -> SqlFunction:
        return SqlFunction("NULLIF", field, *default_values)

    def _get_default_terms(
        self,
        function_arg: ExpressionResult,
        default_results: list[ExpressionResult],
        expression_context: ExpressionContext,
    ) -> list[Any]:
        terms = super()._get_default_terms(function_arg, default_results, expression_context)
        return FunctionArguments.get_typed_literal_terms(terms, expression_context)
