from __future__ import annotations

from typing import Any

from hare.query.expressions import Expression, Function
from hare.query.expressions.compatibility.output_field_compatibility import OutputFieldCompatibility
from hare.query.expressions.compatibility.result_branch import ResultBranch
from hare.query.expressions.enums import NumericValueType
from hare.query.expressions.expression_context import ExpressionContext
from hare.query.expressions.expression_result import ExpressionResult
from hare.query.expressions.numeric.numeric_typing import NumericTyping
from hare.query.expressions.value import Value
from hare.query.functions.comparison.function_arguments import FunctionArguments
from hare.sql.terms.case.case import Case as SqlCase
from hare.sql.terms.functions.function import Function as SqlFunction
from hare.sql.terms.term import Term
from hare.sql.terms.values.null_value import NullValue


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

    def _get_function_field(self, term: Term | str, *default_values: Any) -> SqlFunction:
        return SqlFunction("NULLIF", term, *default_values)

    def _get_result_term(
        self,
        term: SqlFunction,
        function_arg: ExpressionResult,
        default_results: list[ExpressionResult],
        expression_context: ExpressionContext,
    ) -> Term:
        OutputFieldCompatibility.raise_if_value_types_mix(
            [
                ResultBranch.of(argument, result.output_field)
                for argument, result in zip(
                    (self.field, *self.default_values), (function_arg, *default_results), strict=True
                )
            ],
            "NullIf()",
        )
        # Two numbers, one of them a Decimal, are compared as the dialect compares Decimals - a Decimal
        # stored as text would be compared by NULLIF with a number or other decimal text as text.
        numeric_types = [
            NumericTyping.get_field_type(result.output_field) for result in (function_arg, *default_results)
        ]
        value_types = {numeric_type.type for numeric_type in numeric_types if numeric_type is not None}
        if None in numeric_types or NumericValueType.DECIMAL not in value_types:
            return term
        renderers = expression_context.dialect.renderers
        value_term, other_term = term.args
        only_decimals = value_types == {NumericValueType.DECIMAL}
        compared_value = renderers.get_decimal_compared_term(value_term, only_decimals=only_decimals)
        compared_other = renderers.get_decimal_compared_term(other_term, only_decimals=only_decimals)
        if compared_value is value_term and compared_other is other_term:
            return term
        return SqlCase().when(compared_value.eq(compared_other), NullValue()).else_(value_term)

    def _get_default_terms(
        self,
        function_arg: ExpressionResult,
        default_results: list[ExpressionResult],
        expression_context: ExpressionContext,
    ) -> list[Any]:
        terms = super()._get_default_terms(function_arg, default_results, expression_context)
        return FunctionArguments.get_typed_literal_terms(terms, expression_context)
