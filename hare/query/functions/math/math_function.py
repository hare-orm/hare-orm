from __future__ import annotations

from decimal import Decimal
from typing import Any, ClassVar

from hare.dialects.enums import ParameterPosition
from hare.exceptions import QueryError
from hare.fields.field import Field
from hare.query.expressions import Expression, F, Function
from hare.query.expressions.enums import NumericValueType
from hare.query.expressions.expression_context import ExpressionContext
from hare.query.expressions.expression_result import ExpressionResult
from hare.query.expressions.numeric.numeric_type import NumericType
from hare.query.expressions.numeric.numeric_typing import NumericTyping
from hare.query.expressions.value import Value
from hare.sql.functions.declarations import MathFunction as MathFunctionTerm
from hare.sql.terms.term import Term
from hare.sql.terms.values.value_wrapper import ValueWrapper


class MathFunction(Function):
    """A numeric function of one or more numbers - a field name, an expression or a literal in any
    position. ``output`` picks the result type: ``same`` keeps the first argument's type, ``numeric``
    keeps a Decimal a Decimal and makes anything else a float, ``remainder`` also keeps integers
    integers, ``float`` is always a float."""

    #: The SQL function name, also the SQLite UDF's name after its prefix.
    function_name: ClassVar[str]
    database_function = MathFunctionTerm
    #: How the result type follows the arguments.
    output: ClassVar[str] = "numeric"
    #: How many arguments the function takes.
    arity: ClassVar[int] = 1

    populate_field_object = True

    def __init__(self, *arguments: str | int | float | Decimal | Expression | Term) -> None:
        """
        Args:
            arguments: The numbers - field names, expressions or literals.

        Raises:
            QueryError: The number of arguments is wrong, or one is a bool or a non-number literal.
        """
        if len(arguments) != self.arity:
            raise QueryError(f"{type(self).__name__}() takes {self.arity} argument(s), got {len(arguments)}")
        converted = [self.get_argument_expression(argument) for argument in arguments]
        super().__init__(converted[0], *converted[1:])

    def get_argument_expression(self, argument: Any) -> Any:
        """An argument as an expression - a string is a field name, a number a literal.

        Raises:
            QueryError: A bool or a literal that isn't a number.
        """
        if isinstance(argument, str):
            return F(argument)
        if isinstance(argument, (Expression, Term)):
            return argument
        if isinstance(argument, bool) or not isinstance(argument, (int, float, Decimal)):
            raise QueryError(f"{type(self).__name__}() takes numbers, got {argument!r}")
        return Value(argument)

    def _get_default_terms(
        self,
        function_arg: ExpressionResult,
        default_results: list[ExpressionResult],
        expression_context: ExpressionContext,
    ) -> list[Any]:
        """Types a literal argument where the dialect says nothing around it does, as the first
        argument is."""
        terms = super()._get_default_terms(function_arg, default_results, expression_context)
        return [
            Value.get_typed_term(term, term.value, ParameterPosition.FUNCTION_ARGUMENT, expression_context.dialect)
            if isinstance(term, ValueWrapper)
            else term
            for term in terms
        ]

    def _get_output_field(
        self, function_arg: ExpressionResult, default_results: list[ExpressionResult]
    ) -> Field[Any] | None:
        """The field the result is decoded through - see the class docstring.

        Returns:
            The result field, or None when an argument's type is unknown.
        """
        if self.output == "float":
            return NumericTyping.FLOAT_OUTPUT_FIELD  # type: ignore[arg-type]
        fields = [result.output_field for result in (function_arg, *default_results)]  # type:ignore[call-overload]
        numeric_types: list[NumericType] = []
        for field in fields:
            numeric_type = NumericTyping.get_field_type(field)
            if numeric_type is None:
                return None
            numeric_types.append(numeric_type)
        if self.output == "same":
            return fields[0]
        value_types = {numeric_type.type for numeric_type in numeric_types}
        if NumericValueType.FLOAT in value_types:
            return NumericTyping.FLOAT_OUTPUT_FIELD  # type: ignore[arg-type]
        if NumericValueType.DECIMAL in value_types:
            return NumericTyping.DECIMAL_QUOTIENT_OUTPUT_FIELD  # type: ignore[arg-type]
        if self.output == "remainder":
            return NumericTyping.INTEGER_OUTPUT_FIELD  # type: ignore[arg-type]
        return NumericTyping.FLOAT_OUTPUT_FIELD  # type: ignore[arg-type]
