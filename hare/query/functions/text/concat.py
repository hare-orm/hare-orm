from __future__ import annotations

from typing import Any

from hare.exceptions import QueryError
from hare.fields.data.boolean_field import BooleanField
from hare.fields.data.temporal.datetime_field import DatetimeField
from hare.fields.generated_field import GeneratedField
from hare.query.expressions import Expression, Function
from hare.query.expressions.enums import NumericValueType
from hare.query.expressions.expression_context import ExpressionContext
from hare.query.expressions.expression_result import ExpressionResult
from hare.query.expressions.numeric.numeric_typing import NumericTyping
from hare.query.expressions.value import Value
from hare.sql import functions
from hare.sql.functions.text.concat_function import ConcatFunction
from hare.sql.terms.term import Term
from hare.sql.terms.values.value_wrapper import ValueWrapper
from hare.time import Timezone


class Concat(Function):
    """Concatenates a field with other fields or constant text, e.g.
    ``Concat("field_name", other_field_or_text, *args)``. SQLite has no native ``CONCAT``
    support."""

    database_function = ConcatFunction

    def __init__(self, field: str | Expression | Term, *default_values: Any) -> None:
        """
        Args:
            field: The first field name or expression.
            default_values: The other arguments - fields/expressions or literals.

        Raises:
            QueryError: An argument is a bytes literal, which has no text form.
        """
        for argument in (field, *default_values):
            if isinstance(argument.value if isinstance(argument, Value) else argument, bytes):
                raise QueryError(f"Concat() can't concatenate a bytes literal: {argument!r}")
        super().__init__(field, *default_values)  # type: ignore[arg-type]

    @staticmethod
    def _get_text_argument(argument: ExpressionResult) -> Any:
        """An argument concatenated as the same text on every backend - a boolean as
        'true'/'false', a float and a Decimal as Postgres writes them (a Decimal with its field's
        scale), a datetime as its stored text.

        Args:
            argument: The resolved argument.

        Returns:
            The term to concatenate.
        """
        output_field = argument.output_field  # type:ignore[call-overload]
        term = argument.term
        if output_field is None or not isinstance(term, Term) or isinstance(term, ValueWrapper):
            return term
        effective_field = GeneratedField.get_effective_field(output_field)
        if isinstance(effective_field, BooleanField):
            return functions.BooleanAsText(term)
        if isinstance(effective_field, DatetimeField):
            return functions.DatetimeAsText(term, Timezone.get_use_timezone())
        numeric_type = NumericTyping.get_field_type(output_field)
        if numeric_type is not None and numeric_type.type is NumericValueType.FLOAT:
            return functions.FloatAsText(term)
        if (
            numeric_type is not None
            and numeric_type.type is NumericValueType.DECIMAL
            and numeric_type.scale is not None
        ):
            return functions.DecimalAsText(term, numeric_type.scale)
        return term

    def _wrap_argument(
        self, expression_context: ExpressionContext, function_arg: ExpressionResult
    ) -> ExpressionResult:
        return ExpressionResult(
            term=self._get_text_argument(function_arg),
            joins=function_arg.joins,
            output_field=function_arg.output_field,  # type:ignore[call-overload]
        )

    def _get_default_terms(
        self,
        function_arg: ExpressionResult,
        default_results: list[ExpressionResult],
        expression_context: ExpressionContext,
    ) -> list[Any]:
        return [self._get_text_argument(value) for value in default_results]
