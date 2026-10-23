from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from hare.exceptions import ValidationError
from hare.fields.validators.validator import Validator


class KeysValidator(Validator):
    """The mapping has every one of ``keys`` - and, with ``strict=True``, no other.

    Args:
        keys: The required keys.
        strict: Also reject a key outside ``keys``.
        message: Overrides the default error message.
    """

    def __init__(self, keys: Iterable[str], strict: bool = False, message: str | None = None) -> None:
        self.keys = frozenset(keys)
        self.strict = strict
        super().__init__(message)

    def __call__(self, value: Any) -> None:
        if not isinstance(value, Mapping):
            raise ValidationError(f"Value must be a mapping, got {type(value).__name__}")
        missing_keys = self.keys - set(value)
        if missing_keys:
            self._raise(f"Some keys were missing: {', '.join(sorted(missing_keys))}")
        extra_keys = set(value) - self.keys
        if self.strict and extra_keys:
            self._raise(f"Some unknown keys were provided: {', '.join(sorted(extra_keys))}")
