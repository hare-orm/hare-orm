from __future__ import annotations

import re
from typing import Any

from hare.classes.value_descriptions import ValueDescriptions
from hare.exceptions import ConfigurationError, ValidationError
from hare.fields.validators.validator import Validator
from hare.native.native_modules import NativeModules


class RegexValidator(Validator):
    """Checks a text matches a regular expression from its start.

    Args:
        pattern: The regular expression.
        flags: Its ``re`` flags.
        message: Replaces the default error message.

    Raises:
        ConfigurationError: The pattern doesn't compile.
    """

    def __init__(self, pattern: str, flags: int | re.RegexFlag, message: str | None = None) -> None:
        try:
            self.regex = re.compile(pattern, flags)
        except (re.error, ValueError, OverflowError) as error:
            raise ConfigurationError(
                f"RegexValidator: {pattern!r} with flags {flags!r} doesn't compile: {error}"
            ) from error
        self.check = NativeModules.rows.ValueCheck.pattern(self.regex, "match", False)
        super().__init__(message)

    def __call__(self, value: Any) -> None:
        if value is None:
            raise ValidationError("Value must not be None")
        if self.check.failure(value) is not None:
            self._raise(
                f"Value '{ValueDescriptions.get_shown_text(value)}' does not match regex '{self.regex.pattern}'"
            )
