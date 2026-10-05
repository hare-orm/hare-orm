from __future__ import annotations

from enum import StrEnum


class FieldBucket(StrEnum):
    """How a field's raw value from a type of connection is read: as the driver gives it, converted by the
    field's Python type, or through the field's own reading."""

    NATIVE = "native"
    DEFAULT = "default"
    COMPLEX = "complex"
