from __future__ import annotations

from typing import TYPE_CHECKING, Any, TypeVar

from hare.classes.value_descriptions import ValueDescriptions
from hare.dialects.clickhouse.fields.constants import FIXED_STRING_MAX_LENGTH, FIXED_STRING_PAD_BYTE
from hare.dialects.clickhouse.fields.narrowing.byte_length_limit import ByteLengthLimit
from hare.exceptions import ConfigurationError, ValidationError
from hare.fields.data.boolean_field import BooleanField
from hare.fields.data.text.char_field import CharField
from hare.fields.data.text.text_field import TextField
from hare.fields.data.text.text_values import TextValues
from hare.fields.enums import NarrowedValueSource
from hare.fields.field import Field

if TYPE_CHECKING:
    from hare.fields.narrowing.narrowing_limit import NarrowingLimit
    from hare.models import Model

TStr = TypeVar("TStr", str, str | None)


class FixedStringField(Field[TStr]):
    """``FixedString(length)`` - a text of at most ``length`` bytes in UTF-8, stored padded with zero
    bytes to ``length`` and read back without them; a text ending in a zero byte of its own is refused,
    as it would be read back without it.

    Args:
        length: The bytes the column holds - from 1 to ``FIXED_STRING_MAX_LENGTH``.

    Raises:
        ConfigurationError: ``length`` isn't an int in its range.
    """

    field_type = str
    COLUMN_TYPE_FROM_DIALECT = True
    keeps_native_db_values = False

    def __init__(self, length: int, **kwargs: Any) -> None:
        if type(length) is not int or not 1 <= length <= FIXED_STRING_MAX_LENGTH:
            raise ConfigurationError(
                f"FixedStringField(length=...) takes an int from 1 to {FIXED_STRING_MAX_LENGTH}, got "
                f"{ValueDescriptions.get_shown_value(length)}"
            )
        self.length = length
        super().__init__(**kwargs)

    @property
    def SQL_TYPE(self) -> str:  # type: ignore[override]
        return f"FixedString({self.length})"

    @property
    def constraints(self) -> dict[str, Any]:
        return {"max_length": self.length}

    def get_written_value_of_another_type(self, value: Any) -> Any:
        TextValues.raise_if_not_text(self, value)
        return super().get_written_value_of_another_type(value)

    def get_narrowing_limit(self, old_field: Field[Any]) -> NarrowingLimit | None:
        if isinstance(old_field, FixedStringField) and self.length >= old_field.length:
            return None
        # A text longer in bytes would be cut off.
        if isinstance(old_field, (CharField, TextField, FixedStringField)):
            source = NarrowedValueSource.TEXT
        elif isinstance(old_field, BooleanField):
            source = NarrowedValueSource.BOOLEAN
        else:
            source = NarrowedValueSource.OTHER
        return ByteLengthLimit(self.length, source)

    def to_python(self, value: Any) -> Any:
        if value is not None and not isinstance(value, str):
            TextValues.raise_if_not_text(self, value)
        return super().to_python(value)

    def to_db_value(self, value: Any, instance: type[Model] | Model | None) -> Any:
        value = super().to_db_value(value, instance)
        if value is None:
            return None
        encoded = value.encode("utf-8")
        if len(encoded) > self.length:
            raise ValidationError(
                f"{self.model_field_name}: {self.get_value_for_message(value)} is {len(encoded)} bytes in UTF-8, "
                f"more than {self.length}"
            )
        if FIXED_STRING_PAD_BYTE in encoded:
            raise ValidationError(
                f"{self.model_field_name}: {self.get_value_for_message(value)} holds a zero byte - a FixedString "
                "pads its value with them and reads them as its end"
            )
        return value

    def from_db_value(self, value: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, str):
            value = value.encode("utf-8")
        return bytes(value).rstrip(FIXED_STRING_PAD_BYTE).decode("utf-8")
