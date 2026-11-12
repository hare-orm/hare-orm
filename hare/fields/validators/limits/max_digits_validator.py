from __future__ import annotations

from decimal import Decimal

from hare.classes.value_descriptions import ValueDescriptions
from hare.exceptions import ValidationError
from hare.fields.validators.enums import DigitsFailure
from hare.fields.validators.validator import Validator
from hare.native.native_modules import NativeModules


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
        self.check = NativeModules.rows.ValueCheck.max_digits(max_digits, decimal_places)
        super().__init__(message)

    def __call__(self, value: Decimal) -> None:
        if value is None:
            raise ValidationError("Value must not be None")
        failure = self.check.failure(value)
        if failure is None:
            return
        reason, count = failure
        if reason == DigitsFailure.NOT_FINITE:
            raise ValidationError(f"Value {ValueDescriptions.get_shown_value(value)} is not a finite decimal")
        shown_value = ValueDescriptions.get_shown_text(value)
        if reason == DigitsFailure.DIGITS:
            self._raise(f"Value '{shown_value}' has {count} digits, more than max_digits={self.max_digits}")
        if reason == DigitsFailure.DECIMAL_PLACES:
            self._raise(
                f"Value '{shown_value}' has {count} decimal places, more than decimal_places={self.decimal_places}"
            )
        self._raise(
            f"Value '{shown_value}' has {count} digits before the decimal point, more than "
            f"{self.max_digits - self.decimal_places} allowed"
        )
