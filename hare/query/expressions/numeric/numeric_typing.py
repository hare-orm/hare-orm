from __future__ import annotations

from collections.abc import Iterable
from decimal import Decimal
from typing import Any, ClassVar, cast

from hare.core.cache import Cache
from hare.fields.base.field import Field
from hare.fields.data.choices.int_enum_field_instance import IntEnumFieldInstance
from hare.fields.data.numeric.big_int_field import BigIntField
from hare.fields.data.numeric.decimal_field import DecimalField
from hare.fields.data.numeric.float_field import FloatField
from hare.fields.data.numeric.int_field import IntField
from hare.fields.data.temporal.time_delta_field import TimeDeltaField
from hare.fields.generated import GeneratedField
from hare.query.expressions.constants import DECIMAL_OUTPUT_FIELD_MAX_DIGITS
from hare.query.expressions.enums import NumericValueType
from hare.query.expressions.numeric.numeric_type import NumericType
from hare.query.expressions.numeric.unrounded_decimal_field import UnroundedDecimalField


class NumericTyping:
    """Result types of numbers combined by arithmetic, ``Case`` and ``Coalesce``: a float operand
    makes the result a float, otherwise a Decimal one makes it a Decimal, otherwise it is an
    integer."""

    #: Shared, long-lived result fields - the statement plans hold an annotation's
    #: output field weakly, so a fresh instance per call would be collected immediately.
    INTEGER_OUTPUT_FIELD: ClassVar[BigIntField[int]] = BigIntField()
    FLOAT_OUTPUT_FIELD: ClassVar[FloatField[float]] = FloatField()
    DECIMAL_QUOTIENT_OUTPUT_FIELD: ClassVar[UnroundedDecimalField] = UnroundedDecimalField(
        max_digits=DECIMAL_OUTPUT_FIELD_MAX_DIGITS, decimal_places=0
    )
    #: (scale,) -> the field a computed Decimal of that scale is decoded through. Room for every
    #: scale there is.
    DECIMAL_OUTPUT_FIELDS: ClassVar[Cache[DecimalField[Decimal]]] = Cache(
        DECIMAL_OUTPUT_FIELD_MAX_DIGITS + 1, holds_sql=False, keyed_by_model=False
    )

    @staticmethod
    def get_effective_field(field: Field[Any]) -> Field[Any]:
        """Unwraps a GeneratedField to the field its value actually has."""
        return field.output_field if isinstance(field, GeneratedField) else field

    @classmethod
    def get_quotient_output_field(cls, field: Field[Any] | None) -> Field[Any] | None:
        """The field an average or a statistic (a standard deviation, a variance) of a field's
        values is decoded through: of integers a float on every backend, of Decimals a Decimal not
        rounded to the field's scale, of anything else the field itself.

        Args:
            field: The field of the values, a generated one included - None when unknown.

        Returns:
            The field of the result.
        """
        if field is None:
            return None
        effective_field = cls.get_effective_field(field)
        if isinstance(effective_field, IntField):
            return cls.FLOAT_OUTPUT_FIELD  # type: ignore[arg-type]
        if isinstance(effective_field, DecimalField):
            return cls.DECIMAL_QUOTIENT_OUTPUT_FIELD  # type: ignore[arg-type]
        return field

    @staticmethod
    def is_model_field(field: Field[Any]) -> bool:
        """Whether a field is a model's own column, not a shared field typing a computed value."""
        return getattr(field, "model", None) is not None

    @staticmethod
    def get_decimal_scale(value: Decimal) -> int | None:
        """Number of digits after the decimal point of a Decimal literal.

        Args:
            value: The literal.

        Returns:
            The scale, or None for a NaN/infinite value.
        """
        exponent = value.as_tuple().exponent
        if not isinstance(exponent, int):
            return None
        return max(0, -exponent)

    @classmethod
    def get_decimal_output_field(cls, scale: int | None) -> DecimalField[Decimal] | None:
        """The shared DecimalField a Decimal result with ``scale`` digits is decoded through.

        Args:
            scale: Digits after the decimal point.

        Returns:
            The field, or None when the scale is unknown or out of range.
        """
        if scale is None or scale > DECIMAL_OUTPUT_FIELD_MAX_DIGITS:
            return None
        output_field = cls.DECIMAL_OUTPUT_FIELDS.get((scale,))
        if output_field is None:
            output_field = DecimalField(max_digits=DECIMAL_OUTPUT_FIELD_MAX_DIGITS, decimal_places=scale)
            cls.DECIMAL_OUTPUT_FIELDS[(scale,)] = output_field
        return cast("DecimalField[Decimal]", output_field)

    @classmethod
    def get_field_type(cls, field: Field[Any] | None, *, timedelta_as_integer: bool = False) -> NumericType | None:
        """The type of number a field's values are.

        Args:
            field: The field.
            timedelta_as_integer: Count a TimeDeltaField as its stored whole microseconds.

        Returns:
            The numeric type, or None when the field doesn't hold numbers.
        """
        if field is None:
            return None
        effective_field = cls.get_effective_field(field)
        if isinstance(effective_field, UnroundedDecimalField):
            return NumericType(NumericValueType.DECIMAL)
        if isinstance(effective_field, DecimalField):
            return NumericType(NumericValueType.DECIMAL, effective_field.decimal_places)
        if isinstance(effective_field, FloatField):
            return NumericType(NumericValueType.FLOAT)
        if isinstance(effective_field, IntField):
            return NumericType(NumericValueType.INTEGER)
        if timedelta_as_integer and isinstance(effective_field, TimeDeltaField):
            return NumericType(NumericValueType.INTEGER)
        return None

    @classmethod
    def get_literal_type(cls, value: Any) -> NumericType | None:
        """The type of number a Python literal is.

        Args:
            value: The literal.

        Returns:
            The numeric type, or None for a bool or a non-number.
        """
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            return NumericType(NumericValueType.INTEGER)
        if isinstance(value, float):
            return NumericType(NumericValueType.FLOAT)
        if isinstance(value, Decimal):
            return NumericType(NumericValueType.DECIMAL, cls.get_decimal_scale(value))
        return None

    @staticmethod
    def get_decimal_scales(numeric_types: Iterable[NumericType]) -> list[int | None]:
        """Scales of the operands of a Decimal result - an integer operand has scale 0.

        Args:
            numeric_types: The operands' numeric types, none of them a float.

        Returns:
            One scale per operand, None where unknown.
        """
        return [
            numeric_type.scale if numeric_type.type is NumericValueType.DECIMAL else 0
            for numeric_type in numeric_types
        ]

    @classmethod
    def get_common_output_field(cls, numeric_types: list[NumericType], fields: list[Field[Any]]) -> Field[Any] | None:
        """The field a value picked from several numbers (a CASE/COALESCE result) is decoded
        through - a float if any is one, else a Decimal with the largest scale if any is one,
        else an integer.

        Args:
            numeric_types: The numeric type of every candidate value.
            fields: The fields of the candidates that have one, in order - the first one of the
                result's own type is reused.

        Returns:
            The result field.
        """
        value_types = {numeric_type.type for numeric_type in numeric_types}
        if NumericValueType.FLOAT in value_types:
            return next(
                (field for field in fields if isinstance(cls.get_effective_field(field), FloatField)),
                cls.FLOAT_OUTPUT_FIELD,  # type: ignore[arg-type]
            )
        if NumericValueType.DECIMAL in value_types:
            decimal_scales = cls.get_decimal_scales(numeric_types)
            if None in decimal_scales:
                return cls.DECIMAL_QUOTIENT_OUTPUT_FIELD  # type: ignore[arg-type]
            scale = max(scale for scale in decimal_scales if scale is not None)
            for field in fields:
                effective_field = cls.get_effective_field(field)
                if (
                    isinstance(effective_field, DecimalField)
                    and not isinstance(effective_field, UnroundedDecimalField)
                    and effective_field.decimal_places == scale
                ):
                    return field
            return cls.get_decimal_output_field(scale)
        return next(
            (field for field in fields if isinstance(cls.get_effective_field(field), IntField)),
            cls.INTEGER_OUTPUT_FIELD,  # type: ignore[arg-type]
        )

    @classmethod
    def is_enum_field(cls, field: Field[Any] | None) -> bool:
        """Whether a field decodes its integer values to enum members."""
        return field is not None and isinstance(cls.get_effective_field(field), IntEnumFieldInstance)
