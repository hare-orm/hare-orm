from __future__ import annotations

from decimal import Decimal

from hare.fields.validators.limits.numeric_validator import NumericValidator


class MinValueValidator(NumericValidator):
    """
    Min value validator for FloatField, IntField, SmallIntField, BigIntField
    """

    def __init__(self, min_value: int | float | Decimal, message: str | None = None) -> None:
        self._validate_type(min_value)
        self.min_value = min_value
        super().__init__(message)

    def __call__(self, value: int | float | Decimal) -> None:
        self._validate_type(value)
        if value < self.min_value:
            self._raise(f"Value should be greater or equal to {self.min_value}")
