from __future__ import annotations

from typing import Any, TypeVar

from hare.exceptions import ValidationError
from hare.fields.field import Field

TStringValue = TypeVar("TStringValue", str, str | None)


class StringField(Field[TStringValue]):
    """Shared string conversion for text columns, independent of their SQL type and limits.

    Concrete fields retain SQL types, native-value declarations and normalization shortcuts.
    Reading driver text converts it before calling the concrete field's to_python override.
    """

    def get_written_value_of_another_type(self, value: Any) -> Any:
        StringField.raise_if_not_text(self, value)
        return super().get_written_value_of_another_type(value)

    def to_python(self, value: Any) -> Any:
        if value is not None and not isinstance(value, str):
            StringField.raise_if_not_text(self, value)
        return super().to_python(value)

    def from_db_value(self, value: Any) -> Any:
        return self.to_python(StringField.get_read_text(value))

    @staticmethod
    def raise_if_not_text(field: Field[Any], value: Any) -> None:
        """Reject a non-string using the field's current name in the error."""
        if not isinstance(value, str):
            raise ValidationError(f"{field.model_field_name}: expected a str, got {type(value).__name__}")

    @staticmethod
    def get_read_text(value: Any) -> Any:
        """Keep SQL NULL and strings; interpret other driver values as their text."""
        return value if value is None or isinstance(value, str) else str(value)
