from __future__ import annotations

from enum import StrEnum


class StructurePartType(StrEnum):
    """The entry types in a flat structural SQL key."""

    END = "end"
    SOURCE = "source"
    OPAQUE = "opaque"
    NODE = "node"
    SEQUENCE = "sequence"
    MAPPING = "mapping"
    ENUM = "enum"
    VALUE = "value"
    FLOAT = "float"
    IDENTITY = "identity"
