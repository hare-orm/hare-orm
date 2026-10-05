from __future__ import annotations

from typing import Any

from hare.exceptions import ValidationError
from hare.fields.validators.validator import Validator


class LengthValidator(Validator):
    """The base of the validators of a length: of a text, or of a list, a tuple or bytes (the number of
    elements of an array field)."""

    types = (str, list, tuple, bytes)

    def _validate_type(self, value: Any) -> None:
        if not isinstance(value, self.types):
            raise ValidationError("Value must be a string, list, tuple, or bytes, and is required")
