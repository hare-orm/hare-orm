from __future__ import annotations

from typing import Any

from hare.classes.value_descriptions import ValueDescriptions
from hare.exceptions import ValidationError
from hare.fields.validators.limits.length_validator import LengthValidator
from hare.native.native_modules import NativeModules


class MinLengthValidator(LengthValidator):
    """Checks the length of a text, a list, a tuple or bytes is at least ``min_length``."""

    def __init__(self, min_length: int, message: str | None = None) -> None:
        self.min_length = min_length
        self.check = NativeModules.rows.ValueCheck.min_length(min_length)
        super().__init__(message)

    def __call__(self, value: Any) -> None:
        if type(value) not in self.exact_types:
            if value is None:
                raise ValidationError("Value must not be None")
            self._validate_type(value)
        if self.check.failure(value) is not None:
            self._raise(f"Length of '{ValueDescriptions.get_shown_text(value)}' {len(value)} < {self.min_length}")
