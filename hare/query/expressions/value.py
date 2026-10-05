from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from enum import Enum
from typing import TYPE_CHECKING, Any, ClassVar
from uuid import UUID

from hare.dialects.enums import ParameterPosition
from hare.fields.data.binary_field import BinaryField
from hare.fields.data.boolean_field import BooleanField
from hare.fields.data.json.json_field import JSONField
from hare.fields.data.numeric.decimal_field import DecimalField
from hare.fields.data.temporal.date_field import DateField
from hare.fields.data.temporal.datetime_field import DatetimeField
from hare.fields.data.temporal.time_delta_field import TimeDeltaField
from hare.fields.data.temporal.time_field import TimeField
from hare.fields.data.uuid_field import UUIDField
from hare.fields.field import Field
from hare.query.expressions.constants import BIG_INTEGER_LITERAL_RANGE, INTEGER_LITERAL_RANGE, NO_CAST_SQL_TYPES
from hare.query.expressions.numeric.numeric_typing import NumericTyping
from hare.query.expressions.numeric.unrounded_decimal_field import UnroundedDecimalField
from hare.query.expressions.temporal.temporal_arithmetic import TemporalArithmetic
from hare.query.expressions.value_references.expression_arguments import ExpressionArguments
from hare.query.plans.description.declared_plan_parts import DeclaredPlanParts
from hare.query.plans.enums import PlanPartType
from hare.sql.functions.cast import Cast
from hare.sql.terms.term import Term
from hare.sql.terms.values.value_wrapper import ValueWrapper
from hare.sql.types.sql_types import SqlTypes

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.dialect import Dialect
from hare.query.expressions.expression import Expression
from hare.query.expressions.expression_context import ExpressionContext
from hare.query.expressions.expression_result import ExpressionResult


class Value(Expression):
    """
    Wrapper for a value that should be used as a term in a query.
    """

    #: SQL type of a number literal - what it is cast to where its own type has to win over the
    #: type of the term next to it.
    NUMBER_LITERAL_SQL_TYPE: ClassVar[dict[type, str]] = {
        int: SqlTypes.BIGINT,
        float: SqlTypes.FLOAT,
        Decimal: SqlTypes.NUMERIC,
    }

    #: Long-lived output fields of bare literals - the plans hold an output field weakly. Checked in
    #: order: `bool` subclasses `int`, `datetime` subclasses `date`.
    LITERAL_OUTPUT_FIELDS: ClassVar[tuple[tuple[type, Field[Any]], ...]] = (
        (bool, BooleanField()),
        (int, NumericTyping.INTEGER_OUTPUT_FIELD),  # type: ignore[arg-type]
        (float, NumericTyping.FLOAT_OUTPUT_FIELD),  # type: ignore[arg-type]
        (datetime, DatetimeField()),
        (date, DateField()),
        (time, TimeField()),
        (timedelta, TimeDeltaField()),
        (UUID, UUIDField()),
        (dict, JSONField()),
        (bytes, BinaryField()),
    )

    #: Encodes a dict literal to JSON text, kept alive for the same reason as LITERAL_OUTPUT_FIELDS.
    JSON_LITERAL_FIELD: ClassVar[JSONField[Any]] = JSONField()
    #: Encodes a bytes literal to a dialect's stored form of a binary column.
    BINARY_LITERAL_FIELD: ClassVar[BinaryField[Any]] = BinaryField()
    #: Encodes a UUID literal as a UUIDField writes it on the dialect.
    UUID_LITERAL_FIELD: ClassVar[UUIDField[Any]] = UUIDField()

    #: Decodes a Decimal quotient without rounding it.
    DECIMAL_QUOTIENT_OUTPUT_FIELD: ClassVar[UnroundedDecimalField] = NumericTyping.DECIMAL_QUOTIENT_OUTPUT_FIELD  # type: ignore[arg-type,assignment]

    plan_parts: ClassVar[DeclaredPlanParts] = (("value", PlanPartType.LITERAL),)

    def __init__(self, value: Any) -> None:
        self.value = value

    @staticmethod
    def is_non_zero_number(operand: Any) -> bool:
        """Whether an operand is a literal number other than zero - a divisor the SQL doesn't check.

        Args:
            operand: The operand - a ``Value`` or a bare literal.

        Returns:
            True for an int, float or Decimal literal other than zero.
        """
        value = operand.value if isinstance(operand, Value) else operand
        return isinstance(value, int | float | Decimal) and not isinstance(value, bool) and value != 0

    @staticmethod
    def get_decimal_scale(value: Decimal) -> int | None:
        """Number of digits after the decimal point of a Decimal literal.

        Args:
            value: The literal.

        Returns:
            The scale, or None for a NaN/infinite value.
        """
        return NumericTyping.get_decimal_scale(value)

    @staticmethod
    def get_decimal_output_field(scale: int | None) -> DecimalField[Decimal] | None:
        """The shared DecimalField a Decimal result with ``scale`` digits is decoded through.

        Args:
            scale: Digits after the decimal point.

        Returns:
            The field, or None when the scale is unknown or out of range.
        """
        return NumericTyping.get_decimal_output_field(scale)

    @classmethod
    def get_literal_output_field(cls, value: Any) -> Field[Any] | None:
        """The field a bare literal's selected value is decoded through.

        Args:
            value: The literal.

        Returns:
            A shared field of the literal's own type, or None for a str or an unknown type.
        """
        if isinstance(value, Decimal):
            return cls.get_decimal_output_field(cls.get_decimal_scale(value))
        for literal_type, output_field in cls.LITERAL_OUTPUT_FIELDS:
            if isinstance(value, literal_type):
                return output_field
        return None

    @classmethod
    def get_literal_encoder(cls, value: Any, dialect: Dialect) -> Callable[[Any], Any] | None:
        """The conversion a literal needs before it is bound - the stored form of its field type.

        Args:
            value: The literal.
            dialect: The dialect of the database the query runs on.

        Returns:
            The encoder for a datetime or a time (the value a ``DatetimeField``/``TimeField`` write
            binds, following ``use_timezone`` and the configured zone), a timedelta (whole
            microseconds), a UUID (text), a dict (JSON text) or bytes where the dialect stores them in
            a form of its own, else None.
        """
        if isinstance(value, datetime):
            return TemporalArithmetic.get_field_encoder(TemporalArithmetic.DATETIME_OUTPUT_FIELD, dialect)  # type: ignore[arg-type]
        if isinstance(value, time):
            return TemporalArithmetic.get_field_encoder(TemporalArithmetic.TIME_OUTPUT_FIELD, dialect)  # type: ignore[arg-type]
        if isinstance(value, timedelta):
            return TemporalArithmetic.get_field_encoder(TemporalArithmetic.TIMEDELTA_OUTPUT_FIELD, dialect)  # type: ignore[arg-type]
        if isinstance(value, UUID):
            # As a UUIDField writes it - its text, or the UUID itself where the dialect writes one
            # (a UUID compared with a UUID column as one).
            return TemporalArithmetic.get_field_encoder(cls.UUID_LITERAL_FIELD, dialect)  # type: ignore[arg-type]
        if isinstance(value, dict):
            return TemporalArithmetic.get_field_encoder(cls.JSON_LITERAL_FIELD, dialect)  # type: ignore[arg-type]
        if isinstance(value, bytes) and dialect.types.get_db_converter(BinaryField) is not None:
            # Read back as a binary column's value is - from the dialect's stored form of it.
            return TemporalArithmetic.get_field_encoder(cls.BINARY_LITERAL_FIELD, dialect)  # type: ignore[arg-type]
        return None

    @classmethod
    def get_literal_structure(cls, value: Any) -> tuple[Any, ...]:
        """The parts of a literal, besides its value, that change the SQL built or the field its
        result is decoded through.

        Args:
            value: The literal.

        Returns:
            The literal's type, plus its scale for a Decimal, its awareness for a datetime or the type
            an integer beyond 32 bits is cast to.
        """
        if isinstance(value, Decimal):
            return (Decimal, cls.get_decimal_scale(value))
        if isinstance(value, datetime):
            return (datetime, value.tzinfo is not None)
        if type(value) is int:
            return (int, cls.get_cast_sql_type(value, NO_CAST_SQL_TYPES))
        return (type(value),)

    @classmethod
    def get_typed_term(cls, term: Term, value: Any, position: ParameterPosition, dialect: Dialect) -> Term:
        """A bound literal's term, cast where the dialect says nothing around it types the parameter.

        Args:
            term: The literal's term.
            value: The literal.
            position: Where the literal stands in the statement.
            dialect: The dialect of the database the query runs on.

        Returns:
            ``term`` itself, or its cast.
        """
        cast_sql_type = dialect.parameters.get_parameter_cast_type(cls.get_bound_value(value), position)
        return term if cast_sql_type is None else Cast(term, cast_sql_type)

    @staticmethod
    def get_bound_value(value: Any) -> Any:
        """The value a literal binds as - an enum member binds as its value.

        Args:
            value: The literal.

        Returns:
            The literal, or the value of an enum member.
        """
        while isinstance(value, Enum):
            value = value.value
        return value

    @classmethod
    def get_cast_sql_type(cls, value: Any, cast_sql_types: Mapping[type, str]) -> str | None:
        """SQL type a literal is cast to, looked up by the type of the value it binds as.

        Args:
            value: The literal.
            cast_sql_types: SQL type per Python type.

        Returns:
            The SQL type, or None when the literal needs no cast.
        """
        bound_value = cls.get_bound_value(value)
        cast_sql_type = cast_sql_types.get(type(bound_value))
        if cast_sql_type is None and type(bound_value) is int:
            # Beyond the integer a database reads it as beside an integer column.
            if not INTEGER_LITERAL_RANGE[0] <= bound_value <= INTEGER_LITERAL_RANGE[1]:
                in_big_range = BIG_INTEGER_LITERAL_RANGE[0] <= bound_value <= BIG_INTEGER_LITERAL_RANGE[1]
                return SqlTypes.BIGINT if in_big_range else SqlTypes.NUMERIC
        return cast_sql_type

    def get_value_field(self, result: ExpressionResult) -> Field[Any] | None:
        """The shared field of the literal's own type - see ``get_literal_output_field()``."""
        return self.get_literal_output_field(self.value)

    def get_result(self, expression_context: ExpressionContext) -> ExpressionResult:
        return self.get_encoded_result(
            expression_context, self.get_literal_encoder(self.value, expression_context.dialect)
        )

    def get_encoded_result(
        self, expression_context: ExpressionContext, encoder: Callable[[Any], Any] | None
    ) -> ExpressionResult:
        """Resolves this literal, converting it with ``encoder`` first when one is given.

        Args:
            expression_context: The context being resolved.
            encoder: Converts the raw value into what gets bound as the query parameter.

        Returns:
            The resolved literal term.
        """
        wrapper = ValueWrapper(self.value if encoder is None else encoder(self.value))
        if expression_context.value_wrapper_references is not None:
            # See LiteralValueReference's own docstring - recorded unconditionally (every Value(...)
            # binds the same way, unlike a filter kwarg, where whether a plan can bind it depends
            # on which lookup/value_encoder produced the criterion).
            ExpressionArguments.record_literal(
                expression_context.value_wrapper_references, self, "value", wrapper, encoder
            )
        return ExpressionResult(term=wrapper)
