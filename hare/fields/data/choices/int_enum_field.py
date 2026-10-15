from __future__ import annotations

from enum import IntEnum
from typing import Any, TypeVar

from hare.fields.data.choices.int_enum_field_instance import IntEnumFieldInstance

IntEnumType = TypeVar("IntEnumType", bound=IntEnum)


def IntEnumField(
    enum_type: type[IntEnumType],
    description: str | None = None,
    **kwargs: Any,
) -> IntEnumType:
    """A field holding a member of an integer enum - a valid value of the enum is accepted too.

    Args:
        enum_type: The enum class.
        description: The field's description - "name: value" lines by default.
    """
    return IntEnumFieldInstance(enum_type, description, **kwargs)  # type: ignore[return-value]
