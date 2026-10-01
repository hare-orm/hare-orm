from __future__ import annotations

from enum import Enum
from typing import Any, TypeVar

from hare.fields.data.choices.char_enum_field_instance import CharEnumFieldInstance

CharEnumType = TypeVar("CharEnumType", bound=Enum)


def CharEnumField(
    enum_type: type[CharEnumType],
    description: str | None = None,
    max_length: int = 0,
    **kwargs: Any,
) -> CharEnumType:
    """A field holding a member of a string enum - a valid value of the enum is accepted too.

    Args:
        enum_type: The enum class.
        description: The field's description - "name: value" lines by default.
        max_length: The column length - taken from the longest value when zero or omitted, so
            changing the enum may need a schema change.
    """

    return CharEnumFieldInstance(enum_type, description, max_length, **kwargs)  # type: ignore[return-value]
