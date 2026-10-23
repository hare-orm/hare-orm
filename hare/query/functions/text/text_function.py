from __future__ import annotations

from typing import Any, ClassVar

from hare.dialects.enums import ParameterPosition
from hare.exceptions import QueryError
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

    #: The SQL function name, also the name of hare's function after its prefix where a dialect
    #: registers one.
    function_name: ClassVar[str]
    database_function = TextFunctionTerm
    #: How many arguments the function takes, at least and at most.
    arity: ClassVar[tuple[int, int]] = (1, 1)
    #: The types of the arguments after the text - a length, a position or a count is an integer, a
    #: searched or a filling text is text.
    argument_types: ClassVar[tuple[CastType, ...]] = ()
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
        converted = [
            self.get_argument_expression(argument, self.argument_types[index]) for index, argument in enumerate(others)
        ]
        super().__init__(F(first) if isinstance(first, str) else first, *converted)

    @staticmethod
    def get_argument_expression(argument: Any, argument_type: CastType) -> Any:
        """A later argument as an expression - a literal becomes a ``Value``.

        Args:
            argument: The argument.
            argument_type: Its type - ``integer`` or ``text``.

        Raises:
            QueryError: A literal that isn't of its type (a bool is no integer).
        """
        if isinstance(argument, (Expression, Term)):
            return argument
        if argument_type == CastType.INTEGER:
            if isinstance(argument, bool) or not isinstance(argument, int):
                raise QueryError(f"expected an integer or an expression, got {argument!r}")
        elif not isinstance(argument, str):
            raise QueryError(f"expected text or an expression, got {argument!r}")
        return Value(argument)

    def _get_default_terms(
        self,
        function_arg: ExpressionResult,
        default_results: list[ExpressionResult],
        expression_context: ExpressionContext,
    ) -> list[Any]:
        """Types a literal argument where the dialect says nothing around it does, and an integer
        expression as an integer literal there - a database may have no text function taking a
        ``bigint``."""
        terms = super()._get_default_terms(function_arg, default_results, expression_context)
        dialect = expression_context.dialect
        integer_sql_type = dialect.parameters.get_parameter_cast_type(0, ParameterPosition.TEXT_FUNCTION_ARGUMENT)
        typed_terms = []
        for index, (term, result) in enumerate(zip(terms, default_results, strict=True)):
            if isinstance(term, ValueWrapper):
                term = Value.get_typed_term(term, term.value, ParameterPosition.TEXT_FUNCTION_ARGUMENT, dialect)
            elif (
                integer_sql_type is not None
                and FunctionArguments.get_cast_type(result.output_field) == CastType.INTEGER
            ):
                term = Cast(term, integer_sql_type)
            typed_terms.append(term)
        return typed_terms

    def _get_output_field(
        self, function_arg: ExpressionResult, default_results: list[ExpressionResult]
    ) -> Field[Any] | None:
        if self.returns_integer:
            return self.INTEGER_OUTPUT_FIELD
        return self.TEXT_OUTPUT_FIELD
