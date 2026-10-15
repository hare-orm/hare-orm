from __future__ import annotations

from typing import Any, ClassVar

from hare.dialects.enums import ParameterPosition
from hare.exceptions import QueryError
from hare.fields.data.constants import INT32_MAX, INT32_MIN
from hare.fields.data.numeric.int_field import IntField
from hare.fields.data.text.text_field import TextField
from hare.fields.field import Field
from hare.query.expressions import Expression, F, Function
from hare.query.expressions.compatibility.constants import NON_TEXT_VALUE_TYPES
from hare.query.expressions.expression_context import ExpressionContext
from hare.query.expressions.expression_result import ExpressionResult
from hare.query.expressions.value import Value
from hare.query.functions.comparison.function_arguments import FunctionArguments
from hare.sql.enums import CastType
from hare.sql.functions.cast import Cast
from hare.sql.functions.declarations import TextFunction as TextFunctionTerm
from hare.sql.terms.term import Term
from hare.sql.terms.values.value_wrapper import ValueWrapper


class TextFunction(Function):
    """A text function. The first argument is a field name, an expression or a literal; a later
    string argument is text (``Replace("name", "old", "new")``) - wrap a field in ``F()`` there."""

    #: The SQL function name, also the SQLite UDF's name after its prefix.
    function_name: ClassVar[str]
    database_function = TextFunctionTerm
    #: How many arguments the function takes, at least and at most.
    arity: ClassVar[tuple[int, int]] = (1, 1)
    #: The result's type.
    returns_integer: ClassVar[bool] = False
    refused_argument_value_types = NON_TEXT_VALUE_TYPES

    #: Shared, long-lived instances - the statement plans hold an annotation's output
    #: field weakly, so a fresh instance per call would be collected immediately.
    TEXT_OUTPUT_FIELD = TextField()
    INTEGER_OUTPUT_FIELD = IntField()

    populate_field_object = True

    def __init__(self, *arguments: Any) -> None:
        """
        Args:
            arguments: The text first, then the function's other arguments.

        Raises:
            QueryError: The number of arguments is wrong.
        """
        least, most = self.arity
        if not least <= len(arguments) <= most:
            expected = str(least) if least == most else f"{least} to {most}"
            raise QueryError(f"{type(self).__name__}() takes {expected} argument(s), got {len(arguments)}")
        first, *others = arguments
        converted = [self.get_argument_expression(argument) for argument in others]
        super().__init__(F(first) if isinstance(first, str) else first, *converted)

    @staticmethod
    def get_argument_expression(argument: Any) -> Any:
        """A later argument as an expression - a literal becomes a ``Value``.

        Raises:
            QueryError: A bool, a literal that is neither text nor an integer, or an integer
                beyond 32 bits - a length, a position or a count is Postgres's ``integer``.
        """
        if isinstance(argument, (Expression, Term)):
            return argument
        if isinstance(argument, bool) or not isinstance(argument, (str, int)):
            raise QueryError(f"expected text, an integer or an expression, got {argument!r}")
        if isinstance(argument, int) and not INT32_MIN <= argument <= INT32_MAX:
            raise QueryError(f"{argument} is out of range for a 32-bit integer argument")
        return Value(argument)

    def _get_default_terms(
        self,
        function_arg: ExpressionResult,
        default_results: list[ExpressionResult],
        expression_context: ExpressionContext,
    ) -> list[Any]:
        """Types a literal argument where the dialect says nothing around it does, and an integer
        expression as an integer literal there - Postgres has no text function taking a ``bigint``."""
        terms = super()._get_default_terms(function_arg, default_results, expression_context)
        dialect = expression_context.dialect
        integer_sql_type = dialect.parameters.get_parameter_cast_type(0, ParameterPosition.TEXT_FUNCTION_ARGUMENT)
        typed_terms = []
        for term, result in zip(terms, default_results, strict=True):
            if isinstance(term, ValueWrapper):
                term = Value.get_typed_term(term, term.value, ParameterPosition.TEXT_FUNCTION_ARGUMENT, dialect)
            elif (
                integer_sql_type is not None
                and FunctionArguments.get_cast_type(result.output_field) == CastType.INTEGER  # type: ignore[call-overload]
            ):
                term = Cast(term, integer_sql_type)
            typed_terms.append(term)
        return typed_terms

    def _get_output_field(
        self, function_arg: ExpressionResult, default_results: list[ExpressionResult]
    ) -> Field[Any] | None:
        if self.returns_integer:
            return self.INTEGER_OUTPUT_FIELD  # type:ignore[call-overload]
        return self.TEXT_OUTPUT_FIELD  # type:ignore[call-overload]
