from __future__ import annotations

from typing import Any

from hare.classes.value_descriptions import ValueDescriptions
from hare.exceptions import ValidationError
from hare.fields.validators.limits.length_validator import LengthValidator
from hare.native.native_modules import NativeModules


class MaxLengthValidator(LengthValidator):
    """Checks the length of a text, a list, a tuple or bytes is at most ``max_length``."""

    def __init__(self, max_length: int, message: str | None = None) -> None:
        self.max_length = max_length
        self.check = NativeModules.rows.ValueCheck.max_length(max_length)
        super().__init__(message)

    def __call__(self, value: Any) -> None:
        if type(value) not in self.exact_types:
            if value is None:
                raise ValidationError("Value must not be None")
            self._validate_type(value)
        if self.check.failure(value) is not None:
            self._raise(f"Length of '{ValueDescriptions.get_shown_text(value)}' {len(value)} > {self.max_length}")
