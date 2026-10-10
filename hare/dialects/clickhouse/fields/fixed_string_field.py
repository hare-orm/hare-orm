from __future__ import annotations

from typing import TYPE_CHECKING, Any, TypeVar

from hare.dialects.clickhouse.enums import ClickhouseDialectName
from hare.dialects.clickhouse.fields.constants import FIXED_STRING_MAX_LENGTH, FIXED_STRING_PAD_BYTE
from hare.exceptions import ConfigurationError, ValidationError
from hare.fields.field import Field

if TYPE_CHECKING:  # pragma: nocoverage
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
    SUPPORTED_DIALECTS = frozenset({ClickhouseDialectName.CLICKHOUSE})
    keeps_native_db_values = False

    def __init__(self, length: int, **kwargs: Any) -> None:
        if type(length) is not int or not 1 <= length <= FIXED_STRING_MAX_LENGTH:
            raise ConfigurationError(
                f"FixedStringField(length=...) takes an int from 1 to {FIXED_STRING_MAX_LENGTH}, got {length!r}"
            )
        self.length = length
        super().__init__(**kwargs)

    @property
    def SQL_TYPE(self) -> str:  # type: ignore[override]
        return f"FixedString({self.length})"

    @property
    def constraints(self) -> dict[str, Any]:
        return {"max_length": self.length}

    def to_db_value(self, value: Any, instance: type[Model] | Model) -> Any:
        value = super().to_db_value(value, instance)
        if value is None:
            return None
        encoded = value.encode("utf-8")
        if len(encoded) > self.length:
            raise ValidationError(
                f"{self.model_field_name}: {self.get_value_for_message(value)} is {len(encoded)} bytes in UTF-8, "
                f"more than {self.length}"
            )
        if encoded.endswith(FIXED_STRING_PAD_BYTE):
            raise ValidationError(
                f"{self.model_field_name}: a FixedString value ends in no zero byte - it pads with them"
            )
        return value

    def from_db_value(self, value: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, str):
            value = value.encode("utf-8")
        return bytes(value).rstrip(FIXED_STRING_PAD_BYTE).decode("utf-8")
