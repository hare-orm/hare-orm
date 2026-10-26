from __future__ import annotations

from decimal import ROUND_HALF_UP, Context, Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any, Literal, TypeVar, overload

from hare.exceptions import ConfigurationError, ValidationError
from hare.fields.constants import (
    DECIMAL_QUANTIZE_MIN_CONTEXT_PRECISION,
)
from hare.fields.field import Field
from hare.fields.narrowing.decimal_digits_limit import DecimalDigitsLimit
from hare.fields.narrowing.narrowing_limit import NarrowingLimit
from hare.fields.validators.limits.max_digits_validator import MaxDigitsValidator

if TYPE_CHECKING:
    from hare.models import Model
from hare.fields.data.numeric.float_field import FloatField

TDecimal = TypeVar("TDecimal", Decimal, Decimal | None)


class DecimalField(Field[TDecimal]):
    """An exact decimal field - only on a database that stores exact decimals.

    Args:
        max_digits: The most significant digits.
        decimal_places: How many of them come after the decimal point.
    """

    field_type = Decimal

    COLUMN_TYPE_FROM_DIALECT = True
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
        # what max_digits may ask - and rounds a value of more places half away from zero.
        self.quantize_context = Context(
            prec=max(self.max_digits, DECIMAL_QUANTIZE_MIN_CONTEXT_PRECISION), rounding=ROUND_HALF_UP
        )
        self.validators.append(MaxDigitsValidator(self.max_digits, self.decimal_places))

    def _quantize(self, value: Any) -> Decimal:
        if type(value) is Decimal:
            decimal_value = value
        else:
            try:
                decimal_value = Decimal(value)
            except (InvalidOperation, ValueError, TypeError):
                raise ValidationError(
                    f"{self.model_field_name}: {self.get_value_for_message(value)} is not a valid decimal number"
                ) from None
        try:
            quantized_value = decimal_value.quantize(self.quant, context=self.quantize_context)
        except InvalidOperation:
            # quantize() names no reason - an infinity, or more digits than its context holds.
            shown_value = self.get_value_for_message(value)
            if not decimal_value.is_finite():
                raise ValidationError(f"{self.model_field_name}: {shown_value} is not a finite decimal") from None
            raise ValidationError(
                f"{self.model_field_name}: {shown_value} has more digits than max_digits={self.max_digits} holds"
            ) from None
        return quantized_value

    def raise_if_flag(self, value: Any) -> None:
        """Refuses a bool - Decimal(True) is 1, a flag given for a number.

        Args:
            value: The value.

        Raises:
            ValidationError: ``value`` is a bool.
        """
        if isinstance(value, bool):
            raise ValidationError(
                f"{self.model_field_name}: {self.get_value_for_message(value)} is not a valid decimal number"
            )

    def to_python(self, value: Any) -> Decimal | None:
        if value is not None:
            self.raise_if_flag(value)
            # Quantized, not normalized - normalize() strips trailing zeros (500.00 -> 5E+2), unlike
            # a read value.
            value = self._quantize(value)
        return value

    def from_db_value(self, value: Any) -> Decimal | None:
        if isinstance(value, bool):
            # A value read back is no flag given for a number - the check is for given values.
            value = int(value)
        if type(value) is Decimal and value.is_finite():
            digits = value.adjusted() + 1 + self.decimal_places
            if digits > self.quantize_context.prec:
                # A value the database computed (a SUM of the column) may hold more digits than the
                # column does - read as it is.
                return value.quantize(self.quant, context=Context(prec=digits, rounding=ROUND_HALF_UP))
        return self.to_python(value)

    def get_assign_normalized_types(self) -> frozenset[type]:
        # Any Decimal may still need quantizing to this field's scale.
        return frozenset()

    def to_db_value(self, value: Any, instance: type[Model] | Model | None) -> Decimal | None:
        # A value written without a read (update(), an assignment) is quantized too - Decimal(5.1)
        # carries the float's binary noise.
        if value is not None:
            self.raise_if_flag(value)
            value = self._quantize(value)
        self.validate(value)
        return value

    def to_lookup_value(self, value: Any, instance: type[Model] | Model | None) -> Decimal | None:
        if value is None:
            return None
        self.raise_if_flag(value)
        if isinstance(value, float):
            # A float means the number its shortest repr shows (0.1, not 0.1000000000000000055...),
            # the value a write stores and a numeric column compares it with.
            value = repr(value)
        try:
            lookup_value = Decimal(value)
        except (InvalidOperation, ValueError, TypeError):
            raise ValidationError(
                f"{self.model_field_name}: {self.get_value_for_message(value)} is not a valid decimal number"
            ) from None
        if not lookup_value.is_finite():
            # NaN or an infinity - no value a write stores, compared differently by every database.
            raise ValidationError(
                f"{self.model_field_name}: {self.get_value_for_message(value)} is not a finite decimal"
            )
        # A number beyond every value the column holds compares as the first number past them - and a
        # driver writing a Decimal of too many digits (as 0, say) never sees it.
        if lookup_value.adjusted() >= self.max_digits - self.decimal_places:
            return (Decimal(10) ** (self.max_digits - self.decimal_places)).copy_sign(lookup_value)
        return lookup_value

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
        from hare.fields.data.text.char_field import CharField
        from hare.fields.data.text.text_field import TextField

        if isinstance(old_field, DecimalField):
            if (
                self.decimal_places >= old_field.decimal_places
                and self.max_digits - self.decimal_places >= old_field.max_digits - old_field.decimal_places
            ):
                return None
        elif isinstance(old_field, IntField):
            if Decimal(10) ** (self.max_digits - self.decimal_places) > max(map(abs, old_field.COLUMN_TYPE_RANGE)):
                return None
        elif not isinstance(old_field, (FloatField, CharField, TextField)):
            return None
        # A text's number is read by its digits too - a cast would round the places beyond the field's.
        return DecimalDigitsLimit(self.max_digits, self.decimal_places)
