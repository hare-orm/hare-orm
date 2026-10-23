from __future__ import annotations

from decimal import Decimal

from hare.classes.value_descriptions import ValueDescriptions
from hare.exceptions import ValidationError
from hare.fields.validators.validator import Validator


class MaxDigitsValidator(Validator):
    """Checks a ``Decimal`` has at most ``max_digits`` digits, at most ``decimal_places`` of them after
    the point.

    Args:
        max_digits: The most digits.
        decimal_places: How many of them come after the decimal point.
        message: Replaces each of the three default error messages (too many digits, too many decimal
            places, too many whole digits).
    """

    def __init__(self, max_digits: int, decimal_places: int, message: str | None = None) -> None:
        self.max_digits = max_digits
        self.decimal_places = decimal_places
        super().__init__(message)

    def __call__(self, value: Decimal) -> None:
        if value is None:
            raise ValidationError("Value must not be None")
        sign, digit_tuple, exponent = value.as_tuple()
        if not isinstance(exponent, int):
            raise ValidationError(f"Value {ValueDescriptions.get_shown_value(value)} is not a finite decimal")
        if exponent >= 0:
            digits = len(digit_tuple)
            if digit_tuple != (0,):
                # A positive exponent adds that many trailing zeros.
                digits += exponent
            decimals = 0
        else:
            # A negative exponent past every digit adds leading zeros after the decimal point.
            if abs(exponent) > len(digit_tuple):
                digits = decimals = abs(exponent)
            else:
                digits = len(digit_tuple)
                decimals = abs(exponent)
        whole_digits = digits - decimals

        shown_value = ValueDescriptions.get_shown_text(value)
        if digits > self.max_digits:
            self._raise(f"Value '{shown_value}' has {digits} digits, more than max_digits={self.max_digits}")
        if decimals > self.decimal_places:
            self._raise(
                f"Value '{shown_value}' has {decimals} decimal places, more than decimal_places={self.decimal_places}"
            )
        if whole_digits > (self.max_digits - self.decimal_places):
            self._raise(
                f"Value '{shown_value}' has {whole_digits} digits before the decimal point, more than "
                f"{self.max_digits - self.decimal_places} allowed"
            )
