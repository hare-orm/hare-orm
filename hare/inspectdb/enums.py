from __future__ import annotations

from enum import StrEnum


class CatalogDiagnosticCode(StrEnum):
    """Why captured catalog metadata could not be represented as a declaration."""

    GENERATED_COLUMN = "generated_column"
    FOREIGN_KEY = "foreign_key"
    INDEX = "index"
    EXCLUSION_CONSTRAINT = "exclusion_constraint"
    CHECK_CONSTRAINT = "check_constraint"
    TRIGGER = "trigger"
    PARTITIONING = "partitioning"
    GRANT = "grant"
