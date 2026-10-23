from __future__ import annotations

import abc
from typing import Any

from hare.exceptions import ValidationError


class Validator(metaclass=abc.ABCMeta):
    """The base of the validators a field runs on its values: a callable raising ``ValidationError``
    for a value it refuses."""

    def __init__(self, message: str | None = None) -> None:
        """Keeps the message the validator's errors use.

        Args:
            message: Replaces the validator's default error message when given.
        """
        self.message = message

    def _raise(self, default_message: str) -> None:
        """Raises ``ValidationError`` with the validator's message, else ``default_message``."""
        raise ValidationError(self.message or default_message)

    @abc.abstractmethod
    def __call__(self, value: Any) -> None:
        """Checks a value.

        Args:
            value: The value.

        Raises:
            ValidationError: The validator refuses it.
        """
