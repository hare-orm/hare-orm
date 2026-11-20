from __future__ import annotations

from decimal import Decimal

from hare.fields.validators.limits.numeric_validator import NumericValidator
from hare.native.native_modules import NativeModules


class MinValueValidator(NumericValidator):
    """Checks a number - an int, a float or a ``Decimal`` - is at least ``min_value``."""

    def __init__(self, min_value: int | float | Decimal, message: str | None = None) -> None:
        self._validate_type(min_value)
        self.min_value = min_value
        self.check = NativeModules.rows.ValueCheck.lower_limit(min_value)
        super().__init__(message)

    def __call__(self, value: int | float | Decimal) -> None:
        if type(value) not in self.exact_types:
            self._validate_type(value)
        if self.check.failure(value) is not None:
            self._raise(f"Value should be greater or equal to {self.min_value}")
