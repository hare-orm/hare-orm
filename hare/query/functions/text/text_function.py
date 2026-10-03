from typing import Any, ClassVar

from hare.dialects.enums import ParameterPosition
from hare.exceptions import QueryError
from hare.fields.base.field import Field
from hare.fields.data.numeric.int_field import IntField
from hare.fields.data.text.text_field import TextField
from hare.query.expressions import Expression, F, Function
from hare.query.expressions.base.expression_context import ExpressionContext
from hare.query.expressions.base.expression_result import ExpressionResult
from hare.query.expressions.base.value import Value
from hare.sql.functions.declarations import TextFunction as TextFunctionTerm
from hare.sql.terms.base.term import Term
from hare.sql.terms.base.value_wrapper import ValueWrapper


class TextFunction(Function):
    """A text function. The first argument is a field name, an expression or a literal; a later
    string argument is text (``Replace("name", "old", "new")``) - wrap a field in ``F()`` there."""

    #: The SQL function name, also the SQLite UDF's name after its prefix.
    function_name: ClassVar[str]
    database_func = TextFunctionTerm
    #: How many arguments the function takes, at least and at most.
    arity: ClassVar[tuple[int, int]] = (1, 1)
    #: The result's type.
    returns_integer: ClassVar[bool] = False

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
            QueryError: A bool, or a literal that is neither text nor an integer.
        """
        if isinstance(argument, (Expression, Term)):
            return argument
        if isinstance(argument, bool) or not isinstance(argument, (str, int)):
            raise QueryError(f"expected text, an integer or an expression, got {argument!r}")
        return Value(argument)

    def _get_default_terms(
        self,
        function_arg: ExpressionResult,
        default_results: list[ExpressionResult],
        expression_context: ExpressionContext,
    ) -> list[Any]:
        """Types a literal argument where the dialect says nothing around it does."""
        terms = super()._get_default_terms(function_arg, default_results, expression_context)
        return [
            Value.get_typed_term(
                term, term.value, ParameterPosition.TEXT_FUNCTION_ARGUMENT, expression_context.dialect
            )
            if isinstance(term, ValueWrapper)
            else term
            for term in terms
        ]

    def _get_output_field(
        self, function_arg: ExpressionResult, default_results: list[ExpressionResult]
    ) -> Field[Any] | None:
        if self.returns_integer:
            return self.INTEGER_OUTPUT_FIELD  # type:ignore[call-overload]
        return self.TEXT_OUTPUT_FIELD  # type:ignore[call-overload]
