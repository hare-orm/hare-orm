from __future__ import annotations

from enum import StrEnum


class DependencyType(StrEnum):
    """What an operation does to what it names, as another operation may depend on it."""

    CREATED = "created"
    REMOVED = "removed"
    CHANGED = "changed"


class DependencySubject(StrEnum):
    """What a dependency names: a model, a field of it, an object it declares (an index, a constraint, a
    trigger, a view...), its options, or what tables are made of - an extension, a schema, an ENUM type."""

    MODEL = "model"
    FIELD = "field"
    SCHEMA_OBJECT = "schema_object"
    OPTIONS = "options"
    EXTENSION = "extension"
    SCHEMA = "schema"
    ENUM_TYPE = "enum_type"
