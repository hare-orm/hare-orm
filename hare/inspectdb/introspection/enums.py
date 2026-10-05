from __future__ import annotations

from enum import StrEnum


class SchemaPartType(StrEnum):
    """What a part of a schema read back from the database is - the unit two schemas are compared by."""

    TABLE = "table"
    COLUMN = "column"
    PRIMARY_KEY = "primary_key"
    INDEX = "index"
    CONSTRAINT = "constraint"
    FOREIGN_KEY = "foreign_key"
    TRIGGER = "trigger"
    POLICY = "policy"
    GRANT = "grant"
    VIEW = "view"
    MATERIALIZED_VIEW = "materialized_view"
    FUNCTION = "function"
    SEQUENCE = "sequence"
    DICTIONARY = "dictionary"
    TABLE_OPTIONS = "table_options"
    ROW_LEVEL_SECURITY = "row_level_security"
    ENUM_TYPE = "enum_type"
    COMMENT = "comment"
