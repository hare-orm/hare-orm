from __future__ import annotations

from enum import Enum
from typing import Any, TypeVar

from hare.dialects.postgresql.fields.native_enum.native_enum_field_instance import NativeEnumFieldInstance

NativeEnumType = TypeVar("NativeEnumType", bound=Enum)


def NativeEnumField(enum_type: type[NativeEnumType], *, type_name: str | None = None, **kwargs: Any) -> NativeEnumType:
    """A field holding a member of an enum in a PostgreSQL ``ENUM`` column - see
    ``NativeEnumFieldInstance``.

    Args:
        enum_type: The enum class.
        type_name: The ``ENUM`` type's name - the class name in snake case by default.
        kwargs: The arguments of ``CharEnumField``.
    """
    return NativeEnumFieldInstance(enum_type, type_name=type_name, **kwargs)  # type: ignore[return-value]
