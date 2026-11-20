from __future__ import annotations

from typing import Any

from hare.fields.data.text.string_field import StringField


class TextField(StringField[str]):
    """A text of any length - a ``TEXT`` column. It can be unique, indexed and named by a
    ``UniqueConstraint`` or ``Meta.indexes``."""

    field_type = str

    SQL_TYPE = "TEXT"

    def __init__(self, primary_key: bool | None = None, **kwargs: Any) -> None:
        super().__init__(primary_key=primary_key, **kwargs)

    # A text column reads as a str - to_python() checks only a value assigned of another type.
    keeps_native_db_values = True

    def get_assign_normalized_types(self) -> frozenset[type]:
        return self._get_unchanged_assign_types(TextField.to_python, self.field_type)
