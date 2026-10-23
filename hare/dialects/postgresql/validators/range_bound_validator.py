from __future__ import annotations

from typing import Any

from hare.dialects.postgresql.fields.ranges.range import Range
from hare.exceptions import ValidationError
from hare.fields.validators.validator import Validator


class RangeBoundValidator(Validator):
    """Checks one bound of a range against a limit.

    Args:
        limit_value: The limit.
        message: Overrides the default error message.
    """

    def __init__(self, limit_value: Any, message: str | None = None) -> None:
        self.limit_value = limit_value
        super().__init__(message)

    @staticmethod
    def get_range(value: Any) -> Range[Any]:
        """The range a value is - a ``Range``, or a ``(lower, upper)`` pair.

        Raises:
            ValidationError: The value is neither.
        """
        if isinstance(value, Range):
            return value
        if isinstance(value, (tuple, list)) and len(value) == 2:
            return Range(value[0], value[1])
        raise ValidationError(f"Value must be a Range, got {type(value).__name__}")
