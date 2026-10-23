from __future__ import annotations

from decimal import Decimal

from hare.fields.validators.limits.numeric_validator import NumericValidator


class MaxValueValidator(NumericValidator):
    """Checks a number - an int, a float or a ``Decimal`` - is at most ``max_value``."""

    def __init__(self, max_value: int | float | Decimal, message: str | None = None) -> None:
        self._validate_type(max_value)
        self.max_value = max_value
        super().__init__(message)

    def __call__(self, value: int | float | Decimal) -> None:
        self._validate_type(value)
        if value > self.max_value:
            self._raise(f"Value should be less or equal to {self.max_value}")
