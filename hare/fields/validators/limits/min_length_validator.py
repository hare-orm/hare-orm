from __future__ import annotations

from hare.classes.value_descriptions import ValueDescriptions
from hare.exceptions import ValidationError
from hare.fields.validators.limits.length_validator import LengthValidator


class MinLengthValidator(LengthValidator):
    """Checks the length of a text, a list, a tuple or bytes is at least ``min_length``."""

    def __init__(self, min_length: int, message: str | None = None) -> None:
        self.min_length = min_length
        super().__init__(message)

    def __call__(self, value: str) -> None:
        if value is None:
            raise ValidationError("Value must not be None")
        self._validate_type(value)
        if len(value) < self.min_length:
            self._raise(f"Length of '{ValueDescriptions.get_shown_text(value)}' {len(value)} < {self.min_length}")
