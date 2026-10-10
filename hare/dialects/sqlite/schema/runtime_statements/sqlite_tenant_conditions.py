from __future__ import annotations

from typing import TYPE_CHECKING

from hare.dialects.base.schema.runtime_statements.tenant_conditions import TenantConditions

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.sqlite.schema.sqlite_schema_editor import SqliteSchemaEditor


class SqliteTenantConditions(TenantConditions):
    """TenantConditions as SQLite writes it."""

    __slots__ = ()

    editor: SqliteSchemaEditor

    @classmethod
    def get_set_column_count_sql(cls, quoted_columns: list[str]) -> str:
        # A comparison is the integer 0 or 1.
        return "(" + " + ".join(f"({column} IS NOT NULL)" for column in quoted_columns) + ")"
