from __future__ import annotations

import re

from hare.fields.validators.formats.regex_validator import RegexValidator
from hare.fields.validators.validator import Validator


class CommaSeparatedIntegerListValidator(Validator):
    """Checks a text is integers separated by commas (``1,2,3``), negative ones too with
    ``allow_negative``."""

    def __init__(self, allow_negative: bool = False, message: str | None = None) -> None:
        pattern = r"^{neg}\d+(?:{sep}{neg}\d+)*\Z".format(
            neg="(-)?" if allow_negative else "",
            sep=re.escape(","),
        )
        self.regex = RegexValidator(pattern, re.IGNORECASE, message)
        super().__init__(message)

    def __call__(self, value: str) -> None:
        self.regex(value)
