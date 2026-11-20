from __future__ import annotations

from dataclasses import dataclass

from hare.dialects.sqlite.types.constants import (
    SQLITE_DEFAULT_TYPE_AFFINITY,
    SQLITE_EMPTY_TYPE_AFFINITY,
    SQLITE_TYPE_AFFINITY_RULES,
)


@dataclass(frozen=True, slots=True)
class SqlitePhysicalType:
    """A declaration and its storage affinity, without inventing a field's semantics.

    SQLite applies substring rules to the complete declaration, even an unfamiliar
    type name. Preserve that text; parentheses do not declare enforced precision,
    scale or container elements. STRICT table selection uses the same affinity as
    catalog comparison, independently of the introspector's field mapping.
    """

    sql: str
    affinity: str

    @classmethod
    def from_sql(cls, sql: str) -> SqlitePhysicalType:
        """Apply SQLite's ordered affinity rules to a declared column type."""
        upper = sql.upper()
        if not upper.strip():
            return cls(sql, SQLITE_EMPTY_TYPE_AFFINITY)
        for substrings, affinity in SQLITE_TYPE_AFFINITY_RULES:
            if any(substring in upper for substring in substrings):
                return cls(sql, affinity)
        return cls(sql, SQLITE_DEFAULT_TYPE_AFFINITY)
