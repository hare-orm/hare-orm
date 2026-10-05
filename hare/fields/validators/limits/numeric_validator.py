from __future__ import annotations

from decimal import Decimal
from typing import Any

from hare.exceptions import ValidationError
from hare.fields.validators.validator import Validator


class NumericValidator(Validator):
    types = (int, float, Decimal)

    def _validate_type(self, value: Any) -> None:
        if not isinstance(value, self.types):
            raise ValidationError("Value must be a numeric value and is required")
