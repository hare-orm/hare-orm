from __future__ import annotations

import re
from typing import Any

from hare.fields.constants import SLUG_PATTERN, UNICODE_SLUG_PATTERN
from hare.fields.validators.exceptions import InvalidSlug
from hare.fields.validators.validator import Validator


class SlugValidator(Validator):
    """Validates a slug: ASCII letters, digits, hyphens and underscores - any Unicode letter or digit
    too with ``allow_unicode``.

    Args:
        allow_unicode: Accept any Unicode letter or digit.
        message: Replaces the default error message.

    Raises:
        InvalidSlug: The value isn't a slug.
    """

    def __init__(self, allow_unicode: bool = False, message: str | None = None) -> None:
        self.allow_unicode = allow_unicode
        self.regex = re.compile(UNICODE_SLUG_PATTERN if allow_unicode else SLUG_PATTERN)
        super().__init__(message)

    def __call__(self, value: Any) -> None:
        if not isinstance(value, str) or self.regex.fullmatch(value) is None:
            raise InvalidSlug(self.message)


validate_slug = SlugValidator()


validate_slug.__doc__ = "Pre-configured SlugValidator instance."
