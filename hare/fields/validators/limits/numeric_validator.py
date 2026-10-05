from __future__ import annotations

from decimal import Decimal
from typing import Any

from hare.exceptions import ValidationError
from hare.fields.validators.validator import Validator


class NumericValidator(Validator):
    """The base of the validators of a number: an int, a float or a ``Decimal``."""

    types = (int, float, Decimal)

    def _validate_type(self, value: Any) -> None:
        if value is None:
            raise ValidationError("Value must not be None")
        if not isinstance(value, self.types):
            raise ValidationError(f"Value must be a number, got a {type(value).__name__}")
