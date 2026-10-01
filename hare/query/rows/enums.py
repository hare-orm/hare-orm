from __future__ import annotations

from enum import StrEnum


class ReadCodecKind(StrEnum):
    """How ``rust.native.rows.FieldCodec`` reads a column's driver value."""

    #: The value as the driver returns it.
    AS_IS = "as_is"
    #: ``field_type(value)`` unless None or already of that type.
    FIELD_TYPE = "field_type"
    #: The value as it is when exactly of the field's type, else the field's reader.
    EXACT_TYPE = "exact_type"
    BOOLEAN = "boolean"
    BINARY = "binary"
    DATETIME = "datetime"
    DATE = "date"
    TIME = "time"
    TIMEDELTA = "timedelta"
    UUID = "uuid"
    ENUMERATION = "enumeration"
    DECIMAL = "decimal"
    JSON = "json"
    ARRAY = "array"
    RANGE = "range"
    #: The reader given for every value.
    CALL = "call"


class WriteCodecKind(StrEnum):
    """How ``rust.native.rows.FieldCodec`` writes an attribute value."""

    #: A value of exactly the field's type, checked and bound as it is.
    SCALAR = "scalar"
    DATETIME = "datetime"
    DATE = "date"
    TIME = "time"
    TIMEDELTA = "timedelta"
    UUID = "uuid"
    ENUMERATION = "enumeration"
    DECIMAL = "decimal"
    JSON = "json"
    #: The writer given for every value.
    CALL = "call"


class RangeBoundKind(StrEnum):
    """The bounds of a range ``rust.native.rows.FieldCodec`` reads itself."""

    INTEGER = "integer"
    DECIMAL = "decimal"
    DATE = "date"
    #: A timestamp, read into the configured zone.
    DATETIME = "datetime"


class InlineCheckKind(StrEnum):
    """A built-in validator ``rust.native.rows.FieldCodec`` checks itself."""

    MIN_VALUE = "min_value"
    MAX_VALUE = "max_value"
    MIN_LENGTH = "min_length"
    MAX_LENGTH = "max_length"
    MAX_DIGITS = "max_digits"
