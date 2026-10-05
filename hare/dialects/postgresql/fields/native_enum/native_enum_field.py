from __future__ import annotations

from enum import Enum
from typing import Any, TypeVar

from hare.dialects.postgresql.fields.native_enum.native_enum_field_instance import NativeEnumFieldInstance

NativeEnumType = TypeVar("NativeEnumType", bound=Enum)


def NativeEnumField(
    enum_type: type[NativeEnumType], *, type_name: str | None = None, type_schema: str | None = None, **kwargs: Any
) -> NativeEnumType:
    """A field holding a member of an enum in a PostgreSQL ``ENUM`` column - see
    ``NativeEnumFieldInstance``.

    Args:
        enum_type: The enum class.
        type_name: The ``ENUM`` type's name - the class name in snake case by default.
        type_schema: The schema the type lives in - the connection's default schema by default.
        kwargs: The arguments of ``CharEnumField``.
    """
    return NativeEnumFieldInstance(enum_type, type_name=type_name, type_schema=type_schema, **kwargs)  # type: ignore[return-value]
