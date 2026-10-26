from __future__ import annotations

from enum import Enum
from typing import Any, ClassVar

from hare.classes.value_descriptions import ValueDescriptions
from hare.ddl.declared_names import DeclaredNames
from hare.ddl.schema_objects.enum_type import EnumType
from hare.dialects.postgresql.fields.constants import (
    CLASS_NAME_WORD_BOUNDARY_PATTERN,
    NATIVE_ENUM_LABEL_MAX_BYTES,
    NATIVE_ENUM_TYPE_NAME_MAX_LENGTH,
    NATIVE_ENUM_TYPE_NAME_PATTERN,
)
from hare.exceptions import ConfigurationError
from hare.fields.data.choices.char_enum_field_instance import CharEnumFieldInstance


class NativeEnumFieldInstance(CharEnumFieldInstance):
    """A member of an enum in a column of a PostgreSQL ``ENUM`` type of its own - the labels are the
    members' values as text, in the enum's order; the column orders by that order and refuses any
    other label. The type is created, changed and dropped with the fields using it.

    Args:
        enum_type: The enum class.
        type_name: The ``ENUM`` type's name - the enum's class name in snake case by default
            (``OrderStatus`` -> ``order_status``). Fields of one name share the type.
        type_schema: The schema the type lives in - the connection's default one when None, whatever
            the model's ``Meta.schema``; same-named types of two schemas are two types.
        kwargs: The arguments of ``CharEnumField``.

    Raises:
        ConfigurationError: The type name isn't a lowercase identifier of at most 63 characters, the
            schema is empty, holds a null byte or is longer than 63 bytes, the enum has no member, or a
            label is longer than 63 bytes.
    """

    COLUMN_TYPE_FROM_DIALECT = True
    has_native_enum_type: ClassVar[bool] = True

    def __init__(
        self, enum_type: type[Enum], *, type_name: str | None = None, type_schema: str | None = None, **kwargs: Any
    ) -> None:
        labels = tuple(str(member.value) for member in enum_type)
        if not labels:
            raise ConfigurationError(f"NativeEnumField: {enum_type.__name__} has no member")
        super().__init__(enum_type, **kwargs)
        type_name = type_name if type_name is not None else self.get_default_type_name(enum_type)
        if (
            not isinstance(type_name, str)
            or len(type_name) > NATIVE_ENUM_TYPE_NAME_MAX_LENGTH
            or not NATIVE_ENUM_TYPE_NAME_PATTERN.fullmatch(type_name)
        ):
            raise ConfigurationError(
                f"NativeEnumField(type_name=...) takes a lowercase identifier of at most "
                f"{NATIVE_ENUM_TYPE_NAME_MAX_LENGTH} characters, got {ValueDescriptions.get_shown_value(type_name)}"
            )
        if too_long := [label for label in labels if len(label.encode()) > NATIVE_ENUM_LABEL_MAX_BYTES]:
            raise ConfigurationError(
                f"NativeEnumField: an ENUM label is at most {NATIVE_ENUM_LABEL_MAX_BYTES} bytes, got "
                f"{ValueDescriptions.get_shown_text(too_long)}"
            )
        DeclaredNames.raise_if_not_name(type_schema, "NativeEnumField(type_schema=...)", optional=True)
        if type_schema is not None and (
            "\x00" in type_schema or len(type_schema.encode()) > NATIVE_ENUM_TYPE_NAME_MAX_LENGTH
        ):
            raise ConfigurationError(
                f"NativeEnumField(type_schema=...) takes a schema name of at most "
                f"{NATIVE_ENUM_TYPE_NAME_MAX_LENGTH} bytes without a null byte, got "
                f"{ValueDescriptions.get_shown_value(type_schema)}"
            )
        self.type_name = type_name
        self.type_schema = type_schema
        self.requires_enum_type = EnumType(type_name, labels, type_schema)

    @staticmethod
    def get_default_type_name(enum_type: type[Enum]) -> str:
        """The type name of an enum class - its name in snake case."""
        return CLASS_NAME_WORD_BOUNDARY_PATTERN.sub("_", enum_type.__name__).lower()

    @property
    def SQL_TYPE(self) -> str:  # type: ignore[override]
        # The type of its schema, else the one the search path finds.
        if self.type_schema is None:
            return self.type_name
        quoted_schema = self.type_schema.replace('"', '""')
        return f'"{quoted_schema}".{self.type_name}'
