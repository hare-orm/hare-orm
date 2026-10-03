from __future__ import annotations

import datetime
from enum import Enum, IntEnum
from typing import Any

from hare import ConfigurationError
from hare.fields import CharField, IntField
from hare.fields.base import Field
from hare.fields.data.temporal import DateField


class EnumField(CharField):
    """
    An example extension to CharField that serializes Enums
    to and from a Text representation in the DB.
    """

    __slots__ = ("enum_type",)

    def __init__(self, enum_type: type[Enum], **kwargs):
        super().__init__(128, **kwargs)
        if not issubclass(enum_type, Enum):
            raise ConfigurationError(f"{enum_type} is not a subclass of Enum!")
        self.enum_type = enum_type

    def to_db_value(self, value, instance):
        self.validate(value)

        if value is None:
            return None

        if not isinstance(value, self.enum_type):
            raise TypeError(f"Expected type {self.enum_type}, got {value}")

        return value.value

    def to_python(self, value):
        if value is None or isinstance(value, self.enum_type):
            return value

        try:
            return self.enum_type(value)
        except ValueError:
            raise ValueError(f"Database value {value} does not exist on Enum {self.enum_type}.")


class IntEnumField(IntField):
    """
    An example extension to CharField that serializes Enums
    to and from a Text representation in the DB.
    """

    __slots__ = ("enum_type",)

    def __init__(self, enum_type: type[IntEnum], **kwargs):
        super().__init__(**kwargs)
        if not issubclass(enum_type, IntEnum):
            raise ConfigurationError(f"{enum_type} is not a subclass of IntEnum!")
        self.enum_type = enum_type

    def to_db_value(self, value: Any, instance) -> Any:
        self.validate(value)

        if value is None:
            return value
        if not isinstance(value, self.enum_type):
            raise TypeError(f"Expected type {self.enum_type}, got {value}")

        return value.value

    def to_python(self, value: Any) -> Any:
        if value is None or isinstance(value, self.enum_type):
            return value

        try:
            return self.enum_type(value)
        except ValueError:
            raise ValueError(f"Database value {value} does not exist on Enum {self.enum_type}.")


class XorMaskedField(Field[str]):
    """A custom field (subclassing Field directly, like the docs' own custom-field example)
    whose to_db_value()/from_db_value() apply the IDENTICAL XOR-based transform both ways (a
    stand-in for real asymmetric serialization like encryption or masking) - unlike a
    well-behaved field, decoding a DB-read value and encoding a fresh Python value are the SAME
    operation here. Deliberately does NOT override to_python() - relies entirely
    on Field's own default (return the freshly assigned value unchanged), proving that default
    is now safe for an asymmetric field with no per-field opt-out needed."""

    field_type = str
    SQL_TYPE = "VARCHAR(256)"
    XOR_KEY = 0x20  # flips ASCII letter case - keeps output printable and null-byte-free

    def _masked(self, value: str) -> str:
        return "".join(chr(ord(character) ^ self.XOR_KEY) for character in value)

    def to_db_value(self, value: Any, instance: Any) -> Any:
        self.validate(value)
        if value is None:
            return None
        return self._masked(value)

    def from_db_value(self, value: Any) -> Any:
        if value is None:
            return None
        return self._masked(value)


class MarkerDateField(DateField):
    """A DateField subclass whose from_db_value() override records every call it receives -
    lets a test prove whether the override actually ran during hydration (Model.__init__() also
    calls from_db_value() on constructor-provided values, so a plain "is the value still a
    date" check can't tell hydration apart from construction). Guards against a subclass's
    from_db_value() override being silently skipped by DateField's own
    keeps_native_db_values=True (see Field.keeps_native_db_values())."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.hydration_log: list[Any] = []

    def from_db_value(self, value: Any) -> datetime.date | None:
        self.hydration_log.append(value)
        return super().from_db_value(value)
