from typing import Any, ClassVar

from hare.dialects.enums import ParameterPosition
from hare.fields.base.field import Field
from hare.fields.data.boolean import BooleanField
from hare.fields.data.numeric.decimal_field import DecimalField
from hare.fields.data.numeric.float_field import FloatField
from hare.fields.data.numeric.int_field import IntField
from hare.fields.data.temporal.date_field import DateField
from hare.fields.data.temporal.datetime_field import DatetimeField
from hare.fields.data.temporal.time_field import TimeField
from hare.fields.data.text.char_field import CharField
from hare.fields.data.text.text_field import TextField
from hare.query.expressions import Expression, F
from hare.query.expressions.base.expression_context import ExpressionContext
from hare.query.expressions.base.value import Value
from hare.query.expressions.numeric.numeric_typing import NumericTyping
from hare.sql.enums import CastType
from hare.sql.terms.base.term import Term
from hare.sql.terms.base.value_wrapper import ValueWrapper


class FunctionArguments:
    """How the comparison functions read their arguments."""

    #: Field class to the type of value it holds, most specific first.
    CAST_TYPES: ClassVar[tuple[tuple[type, CastType], ...]] = (
        (BooleanField, CastType.BOOLEAN),
        (IntField, CastType.INTEGER),
        (FloatField, CastType.FLOAT),
        (DecimalField, CastType.DECIMAL),
        (CharField, CastType.TEXT),
        (TextField, CastType.TEXT),
        (DatetimeField, CastType.DATETIME),
        (DateField, CastType.DATE),
        (TimeField, CastType.TIME),
    )

    @classmethod
    def get_cast_type(cls, field: Field[Any] | None) -> CastType:
        """The type of value a field holds, ``unknown`` for any other field or none."""
        effective_field = NumericTyping.get_effective_field(field) if field is not None else None
        for field_class, cast_type in cls.CAST_TYPES:
            if isinstance(effective_field, field_class):
                return cast_type
        return CastType.UNKNOWN

    @staticmethod
    def get_expression(argument: Any) -> Any:
        """An argument as an expression - a string is a field name, any other non-expression a literal."""
        if isinstance(argument, str):
            return F(argument)
        if isinstance(argument, (Expression, Term)):
            return argument
        return Value(argument)

    @staticmethod
    def get_typed_literal_terms(terms: list[Any], expression_context: ExpressionContext) -> list[Any]:
        """Types literal terms where the dialect says nothing around them does."""
        return [
            Value.get_typed_term(term, term.value, ParameterPosition.FUNCTION_ARGUMENT, expression_context.dialect)
            if isinstance(term, ValueWrapper)
            else term
            for term in terms
        ]
