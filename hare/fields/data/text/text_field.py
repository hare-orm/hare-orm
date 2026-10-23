from __future__ import annotations

from typing import Any

from hare.fields.data.text.text_values import TextValues
from hare.fields.field import Field


class TextField(Field[str]):
    """A text of any length - a ``TEXT`` column. It can be unique, indexed and named by a
    ``UniqueConstraint`` or ``Meta.indexes``."""

    field_type = str

    SQL_TYPE = "TEXT"

    def __init__(self, primary_key: bool | None = None, **kwargs: Any) -> None:
        super().__init__(primary_key=primary_key, **kwargs)

    # A text column reads as a str - to_python() checks only a value assigned of another type.
    keeps_native_db_values = True

    def get_written_value_of_another_type(self, value: Any) -> Any:
        TextValues.raise_if_not_text(self, value)
        return super().get_written_value_of_another_type(value)

    def to_python(self, value: Any) -> Any:
        if value is not None and not isinstance(value, str):
            TextValues.raise_if_not_text(self, value)
        return super().to_python(value)

    def from_db_value(self, value: Any) -> Any:
        return self.to_python(TextValues.get_read_text(value))
