from __future__ import annotations

from typing import Any
from uuid import UUID

from hare.exceptions import QueryError
from hare.fields.data.temporal.datetime_field import DatetimeField
from hare.fields.data.uuid_field import UUIDField
from hare.fields.generated_field import GeneratedField
from hare.query.expressions import Expression, Function
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
    ``Concat("field_name", other_field_or_text, *args)``."""

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
        super().__init__(Concat.get_uuid_as_text(field), *map(Concat.get_uuid_as_text, default_values))

    @staticmethod
    def get_uuid_as_text(argument: Any) -> Any:
        """An argument with a UUID literal given as its text - a dialect may bind a UUID in a form of
        its own.

        Args:
            argument: The argument.

        Returns:
            The argument, a UUID literal as a text literal.
        """
        value = argument.value if isinstance(argument, Value) else argument
        return Value(str(value)) if isinstance(value, UUID) else argument

    @staticmethod
    def _get_text_argument(argument: ExpressionResult, expression_context: ExpressionContext) -> Any:
        """An argument concatenated as the text its database writes of it - a naive datetime
        (``use_timezone=False``) at the machine's local zone, a UUID a dialect keeps in a form of its
        own as its text, an integer enum's member as its number.

        Args:
            argument: The resolved argument.
            expression_context: The context being resolved.

        Returns:
            The term to concatenate.
        """
        output_field = argument.output_field
        term = argument.term
        if output_field is None or not isinstance(term, Term) or isinstance(term, ValueWrapper):
            return term
        effective_field = GeneratedField.get_effective_field(output_field)
        if isinstance(effective_field, DatetimeField):
            return functions.DatetimeAsText(term, Timezone.get_use_timezone())
        if isinstance(effective_field, UUIDField):
            # A dialect keeping a UUID in a form of its own writes its text into JSON.
            return expression_context.dialect.types.get_json_term(effective_field, term) or term
        if NumericTyping.is_enum_field(effective_field):
            # A database keeping the members by their names would write the name.
            return expression_context.dialect.renderers.get_enum_number_term(term)
        return term

    def _wrap_argument(
        self, expression_context: ExpressionContext, function_arg: ExpressionResult
    ) -> ExpressionResult:
        return ExpressionResult(
            term=self._get_text_argument(function_arg, expression_context),
            joins=function_arg.joins,
            output_field=function_arg.output_field,
        )

    def _get_default_terms(
        self,
        function_arg: ExpressionResult,
        default_results: list[ExpressionResult],
        expression_context: ExpressionContext,
    ) -> list[Any]:
        return [self._get_text_argument(value, expression_context) for value in default_results]
