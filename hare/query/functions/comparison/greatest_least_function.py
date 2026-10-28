from __future__ import annotations

from typing import Any, ClassVar

from hare.exceptions import FieldError, QueryError
from hare.fields.data.json.json_field import JSONField
from hare.fields.field import Field
from hare.fields.generated_field import GeneratedField
from hare.query.expressions import Expression, Function, Value
from hare.query.expressions.compatibility.output_field_compatibility import OutputFieldCompatibility
from hare.query.expressions.compatibility.result_branch import ResultBranch
from hare.query.expressions.expression_context import ExpressionContext
from hare.query.expressions.expression_result import ExpressionResult
from hare.query.expressions.numeric.numeric_type import NumericType
from hare.query.expressions.numeric.numeric_typing import NumericTyping
from hare.query.functions.comparison.function_arguments import FunctionArguments
from hare.sql.functions.greatest_least import GreatestLeast as GreatestLeastTerm
from hare.sql.terms.functions.function import Function as HareSqlFunction
from hare.sql.terms.term import Term


class GreatestLeastFunction(Function):
    """``GREATEST``/``LEAST`` of two or more values, a NULL among them as the database takes it -
    skipped, or the result NULL. A string is a field name."""

    #: ``GREATEST`` or ``LEAST``.
    function_name: ClassVar[str]
    database_function = GreatestLeastTerm
    returns_an_argument = True

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
            numeric_type = NumericTyping.get_field_type(result.output_field)
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
        typed_terms = FunctionArguments.get_typed_literal_terms(terms, expression_context)
        # A wall clock literal beside a wall clock is typed by it - a cast would make it a moment.
        return [
            term if self.binds_as_main_field(Value.get_bound_value(self.get_literal(value)), function_arg) else typed
            for value, term, typed in zip(self.default_values, terms, typed_terms, strict=True)
        ]

    @staticmethod
    def get_literal(value: Any) -> Any:
        """The literal a value given as ``Value(...)`` or bare holds - None for an expression of another sort.

        Args:
            value: A default value.

        Returns:
            The literal.
        """
        if isinstance(value, Value):
            return value.value
        return None if isinstance(value, (Expression, Term)) else value

    def _get_result_term(
        self,
        term: HareSqlFunction,
        function_arg: ExpressionResult,
        default_results: list[ExpressionResult],
        expression_context: ExpressionContext,
    ) -> Term:
        if not expression_context.dialect.features.orders_json_values_in_aggregates and any(
            isinstance(
                GeneratedField.get_effective_field(result.output_field) if result.output_field is not None else None,
                JSONField,
            )
            for result in (function_arg, *default_results)
        ):
            # It would compare the JSON text, as MAX and MIN would (Features.orders_json_values_in_aggregates).
            raise FieldError(
                f"{type(self).__name__}() of a JSON value would compare its text on "
                f"{expression_context.dialect.name} - convert it with Cast()"
            )
        return term

    def get_result(self, expression_context: ExpressionContext) -> ExpressionResult:
        result = super().get_result(expression_context)
        if isinstance(result.term, GreatestLeastTerm):
            output_field: Field[Any] | None = result.output_field
            result.term.compares_numbers = NumericTyping.get_field_type(output_field) is not None
            # Each value as the type of the whole result needs it, as the branches of a CASE.
            renderers = expression_context.dialect.renderers
            result.term.args = [
                renderers.get_result_branch_term(argument, output_field) for argument in result.term.args
            ]
        return result

    def _get_output_field(
        self, function_arg: ExpressionResult, default_results: list[ExpressionResult]
    ) -> Field[Any] | None:
        results = [function_arg, *default_results]
        OutputFieldCompatibility.raise_if_value_types_mix(
            [
                ResultBranch.of(argument, result.output_field)
                for argument, result in zip((self.field, *self.default_values), results, strict=True)
            ],
            f"{type(self).__name__}()",
        )
        fields = [result.output_field for result in results if result.output_field is not None]
        numeric_types = self.get_numeric_types(results)
        if numeric_types is not None:
            return NumericTyping.get_common_output_field(numeric_types, fields)
        return fields[0] if fields else None
