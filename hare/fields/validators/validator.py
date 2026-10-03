import abc
from typing import Any

from hare.exceptions import ValidationError


class Validator(metaclass=abc.ABCMeta):
    def __init__(self, message: str | None = None) -> None:
        """
        Args:
            message: Overrides the validator's default error message when given.
        """
        self.message = message

    def _raise(self, default_message: str) -> None:
        """
        Raise ValidationError with the caller-supplied message, or default_message if none was given.
        """
        raise ValidationError(self.message or default_message)

    @abc.abstractmethod
    def __call__(self, value: Any) -> None:
        """
        All specific validators should implement this method.

        Args:
            value: The given value to be validated.

        Raises:
            ValidationError: if validation failed.
        """
