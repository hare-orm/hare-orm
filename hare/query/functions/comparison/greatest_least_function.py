from typing import Any, ClassVar

from hare.exceptions import QueryError
from hare.fields.base.field import Field
from hare.query.expressions import Function
from hare.query.expressions.base.expression_context import ExpressionContext
from hare.query.expressions.base.expression_result import ExpressionResult
from hare.query.expressions.numeric.numeric_type import NumericType
from hare.query.expressions.numeric.numeric_typing import NumericTyping
from hare.query.functions.comparison.function_arguments import FunctionArguments
from hare.sql.functions.greatest_least import GreatestLeast as GreatestLeastTerm


class GreatestLeastFunction(Function):
    """``GREATEST``/``LEAST`` of two or more values - NULLs are skipped on every backend, the result
    is NULL only when every value is. A string is a field name."""

    #: ``GREATEST`` or ``LEAST``.
    function_name: ClassVar[str]
    database_func = GreatestLeastTerm

    populate_field_object = True

    def __init__(self, *expressions: Any) -> None:
        """
        Args:
            expressions: Field names, expressions or literals.

        Raises:
            QueryError: Fewer than two values.
        """
        if len(expressions) < 2:
            raise QueryError(f"{type(self).__name__}() takes at least two values, got {len(expressions)}")
        converted = [FunctionArguments.get_expression(expression) for expression in expressions]
        super().__init__(converted[0], *converted[1:])

    @staticmethod
    def get_numeric_types(results: list[ExpressionResult]) -> list[NumericType] | None:
        """Every value's numeric type, or None when one isn't a number."""
        numeric_types = []
        for result in results:
            numeric_type = NumericTyping.get_field_type(result.output_field)  # type:ignore[call-overload]
            if numeric_type is None:
                return None
            numeric_types.append(numeric_type)
        return numeric_types

    def _get_default_terms(
        self,
        function_arg: ExpressionResult,
        default_results: list[ExpressionResult],
        expression_context: ExpressionContext,
    ) -> list[Any]:
        terms = super()._get_default_terms(function_arg, default_results, expression_context)
        return FunctionArguments.get_typed_literal_terms(terms, expression_context)

    def get_result(self, expression_context: ExpressionContext) -> ExpressionResult:
        result = super().get_result(expression_context)
        if isinstance(result.term, GreatestLeastTerm):
            result.term.compares_numbers = NumericTyping.get_field_type(result.output_field) is not None  # type:ignore[call-overload]
        return result

    def _get_output_field(
        self, function_arg: ExpressionResult, default_results: list[ExpressionResult]
    ) -> Field[Any] | None:
        results = [function_arg, *default_results]
        fields = [result.output_field for result in results if result.output_field is not None]  # type:ignore[call-overload]
        numeric_types = self.get_numeric_types(results)
        if numeric_types is not None:
            return NumericTyping.get_common_output_field(numeric_types, fields)
        return fields[0] if fields else None
