from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.exceptions import ValidationError
from hare.fields.constants import NULL_BYTE_MESSAGE
from hare.fields.enums import NativeWriteCheck
from hare.fields.field import Field

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.models import Model


class TextField(Field[str]):
    """
    Large Text field.

    Accepts ``unique=True``/``db_index=True`` and can be used in a ``UniqueConstraint``/
    ``Meta.indexes`` - both Postgres and SQLite index a TEXT column natively.
    """

    field_type = str

    SQL_TYPE = "TEXT"

    def __init__(self, primary_key: bool | None = None, **kwargs: Any) -> None:
        super().__init__(primary_key=primary_key, **kwargs)

    native_write_check = NativeWriteCheck.NO_NULL_BYTE

    def to_db_value(self, value: Any, instance: type[Model] | Model) -> Any:
        value = super().to_db_value(value, instance)
        # Same reasoning as CharField's own identical check.
        if isinstance(value, str) and "\x00" in value:
            raise ValidationError(f"{self.model_field_name}: {NULL_BYTE_MESSAGE}")
        return value
