from __future__ import annotations

import functools
from collections.abc import Callable
from decimal import Context, Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any, Literal, TypeVar, overload

from hare.exceptions import ConfigurationError, ValidationError
from hare.fields.constants import (
    DECIMAL_QUANTIZE_MIN_CONTEXT_PRECISION,
)
from hare.fields.field import Field
from hare.fields.narrowing.decimal_digits_limit import DecimalDigitsLimit
from hare.fields.narrowing.narrowing_limit import NarrowingLimit
from hare.fields.validators.limits.max_digits_validator import MaxDigitsValidator
from hare.sql import functions
from hare.sql.terms.term import Term

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.models import Model
from hare.fields.data.numeric.float_field import FloatField

TDecimal = TypeVar("TDecimal", Decimal, Decimal | None)


class DecimalField(Field[TDecimal]):
    """An exact decimal field.

    Args:
        max_digits: The most significant digits.
        decimal_places: How many of them come after the decimal point.
    """

    field_type = Decimal

    keeps_native_db_values = True

    @overload
    def __init__(
        self: DecimalField[Decimal],
        max_digits: int,
        decimal_places: int,
        *,
        null: Literal[False] = False,
        **kwargs: Any,
    ) -> None: ...

    @overload
    def __init__(
        self: DecimalField[Decimal | None],
        max_digits: int,
        decimal_places: int,
        *,
        null: Literal[True],
        **kwargs: Any,
    ) -> None: ...

    def __init__(self, max_digits: int, decimal_places: int, **kwargs: Any) -> None:
        if int(max_digits) < 1:
            raise ConfigurationError("'max_digits' must be >= 1")
        if int(decimal_places) < 0:
            raise ConfigurationError("'decimal_places' must be >= 0")
        if int(decimal_places) > int(max_digits):
            raise ConfigurationError("'decimal_places' must be <= 'max_digits'")
        super().__init__(**kwargs)
        self.max_digits = max_digits
        self.decimal_places = decimal_places
        self.quant = Decimal("1" if decimal_places == 0 else f"1.{('0' * decimal_places)}")
        # quantize() checks the digit count against its context's precision - 28 by default, below
        # what max_digits may ask.
        self.quantize_context = Context(prec=max(self.max_digits, DECIMAL_QUANTIZE_MIN_CONTEXT_PRECISION))
        max_digits_validator = MaxDigitsValidator(self.max_digits, self.decimal_places)
        self.validators.append(max_digits_validator)
        # NUMERIC(max_digits, decimal_places) rejects more integer digits and rounds the scale.
        self.validators_enforced_by_column_type.append(max_digits_validator)

    def get_like_text_function(self) -> Callable[[Term], Term] | None:
        # The decimal padded to its scale, as PostgreSQL writes a NUMERIC column, on every dialect.
        return functools.partial(functions.DecimalAsText, scale=self.decimal_places)

    def _quantize(self, value: Any) -> Decimal:
        try:
            decimal_value = Decimal(value)
        except (InvalidOperation, ValueError, TypeError):
            raise ValidationError(
                f"{self.model_field_name}: {self.get_value_for_message(value)} is not a valid decimal number"
            ) from None
        try:
            quantized_value = decimal_value.quantize(self.quant, context=self.quantize_context)
        except InvalidOperation as error:
            raise ValidationError(f"{self.model_field_name}: {error}")
        # A numeric column has no negative zero - Postgres reads -0.00 back as 0.00.
        return quantized_value.copy_abs() if quantized_value.is_zero() else quantized_value

    def to_python(self, value: Any) -> Decimal | None:
        if value is not None:
            # Quantized, not normalized - normalize() strips trailing zeros (500.00 -> 5E+2), unlike
            # a read value.
            value = self._quantize(value)
        return value

    def get_assign_normalized_types(self) -> frozenset[type]:
        # Any Decimal may still need quantizing to this field's scale.
        return frozenset()

    def to_db_value(self, value: Any, instance: type[Model] | Model) -> Decimal | None:
        # A value written without a read (update(), an assignment) is quantized too - Decimal(5.1)
        # carries the float's binary noise.
        if value is not None:
            value = self._quantize(value)
        self.validate(value)
        return value

    def to_lookup_value(self, value: Any, instance: type[Model] | Model) -> Decimal | None:
        if value is None:
            return None
        if isinstance(value, float):
            # A float means the number its shortest repr shows (0.1, not 0.1000000000000000055...),
            # the value a write stores and a numeric column compares it with on Postgres.
            value = repr(value)
        try:
            return Decimal(value)
        except InvalidOperation as error:
            raise ValidationError(f"{self.model_field_name}: {error}")

    @property
    def constraints(self) -> dict[str, Any]:
        return {
            "max_digits": self.max_digits,
            "decimal_places": self.decimal_places,
        }

    @property
    def SQL_TYPE(self) -> str:  # type: ignore[override]
        return f"DECIMAL({self.max_digits},{self.decimal_places})"

    def get_narrowing_limit(self, old_field: Field[Any]) -> NarrowingLimit | None:
        # Imported here: the modules import each other.
        from hare.fields.data.numeric.int_field import IntField

        if isinstance(old_field, DecimalField):
            if (
                self.decimal_places >= old_field.decimal_places
                and self.max_digits - self.decimal_places >= old_field.max_digits - old_field.decimal_places
            ):
                return None
        elif isinstance(old_field, IntField):
            if Decimal(10) ** (self.max_digits - self.decimal_places) > max(map(abs, old_field.COLUMN_TYPE_RANGE)):
                return None
        elif not isinstance(old_field, FloatField):
            return None
        return DecimalDigitsLimit(self.max_digits, self.decimal_places)
