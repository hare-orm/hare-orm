from __future__ import annotations

from decimal import Decimal

from hare.fields.validators.limits.numeric_validator import NumericValidator
from hare.native.native_modules import NativeModules


class MaxValueValidator(NumericValidator):
    """Checks a number - an int, a float or a ``Decimal`` - is at most ``max_value``."""

    def __init__(self, max_value: int | float | Decimal, message: str | None = None) -> None:
        self._validate_type(max_value)
        self.max_value = max_value
        self.check = NativeModules.rows.ValueCheck.upper_limit(max_value)
        super().__init__(message)

    def __call__(self, value: int | float | Decimal) -> None:
        if type(value) not in self.exact_types:
            self._validate_type(value)
        if self.check.failure(value) is not None:
            self._raise(f"Value should be less or equal to {self.max_value}")
