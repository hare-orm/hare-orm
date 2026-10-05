from __future__ import annotations

from hare.exceptions import ValidationError
from hare.fields.validators.limits.length_validator import LengthValidator


class MaxLengthValidator(LengthValidator):
    """
    A validator to validate the length of given value whether greater than max_length or not.
    """

    def __init__(self, max_length: int, message: str | None = None) -> None:
        self.max_length = max_length
        super().__init__(message)

    def __call__(self, value: str) -> None:
        if value is None:
            raise ValidationError("Value must not be None")
        self._validate_type(value)
        if len(value) > self.max_length:
            self._raise(f"Length of '{value}' {len(value)} > {self.max_length}")
