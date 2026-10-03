import re
from typing import Any

from hare.exceptions import ValidationError
from hare.fields.validators.validator import Validator


class RegexValidator(Validator):
    """
    A validator to validate the given value whether match regex or not.
    """

    def __init__(self, pattern: str, flags: int | re.RegexFlag, message: str | None = None) -> None:
        self.regex = re.compile(pattern, flags)
        super().__init__(message)

    def __call__(self, value: Any) -> None:
        if value is None:
            raise ValidationError("Value must not be None")
        if not self.regex.match(value):
            self._raise(f"Value '{value}' does not match regex '{self.regex.pattern}'")
