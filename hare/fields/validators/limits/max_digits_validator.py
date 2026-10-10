from __future__ import annotations

from decimal import Decimal

from hare.exceptions import ValidationError
from hare.fields.validators.validator import Validator


class MaxDigitsValidator(Validator):
    """
    A validator to validate the total significant digits of a Decimal value whether greater than
    max_digits or not, accounting for decimal_places.

    Args:
        max_digits: The most significant digits.
        decimal_places: How many of them come after the decimal point.
        message: Overrides all three of this validator's default error messages (too many
            total digits, too many decimal places, too many whole digits) with the same
            text - there's no per-check override yet.
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
            raise ValidationError(f"Value '{value}' is not a finite decimal")
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

        if digits > self.max_digits:
            self._raise(f"Value '{value}' has {digits} digits, more than max_digits={self.max_digits}")
        if decimals > self.decimal_places:
            self._raise(
                f"Value '{value}' has {decimals} decimal places, more than decimal_places={self.decimal_places}"
            )
        if whole_digits > (self.max_digits - self.decimal_places):
            self._raise(
                f"Value '{value}' has {whole_digits} digits before the decimal point, more than "
                f"{self.max_digits - self.decimal_places} allowed"
            )
