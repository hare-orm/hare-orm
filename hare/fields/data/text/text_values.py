from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.exceptions import ValidationError

if TYPE_CHECKING:
    from hare.fields.field import Field


class TextValues:
    """A text field takes only a str - a number, a UUID, bytes or any other object would be stored as
    its ``str()``."""

    @staticmethod
    def raise_if_not_text(field: Field[Any], value: Any) -> None:
        """Refuses a value that isn't a str.

        Args:
            field: The text field.
            value: The value.

        Raises:
            ValidationError: ``value`` isn't a str.
        """
        if not isinstance(value, str):
            raise ValidationError(f"{field.model_field_name}: expected a str, got {type(value).__name__}")

    @staticmethod
    def get_read_text(value: Any) -> Any:
        """A text read from the database - a value of another type a driver gives as its text.

        Args:
            value: The value read.

        Returns:
            The text, None for NULL.
        """
        return value if value is None or isinstance(value, str) else str(value)
