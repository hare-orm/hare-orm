from __future__ import annotations

from typing import TYPE_CHECKING

from hare.dialects.base.schema.runtime_statements.table_locks import TableLocks

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.postgresql.schema.postgresql_schema_editor import PostgresqlSchemaEditor


class PostgresqlTableLocks(TableLocks):
    """TableLocks as PostgreSQL writes it."""

    __slots__ = ()

    editor: PostgresqlSchemaEditor

    @classmethod
    def get_lock_table_sql(cls, qualified_table: str) -> str | None:
        return f"LOCK TABLE {qualified_table} IN SHARE ROW EXCLUSIVE MODE"

    @classmethod
    def get_migration_lock_sql(cls) -> str | None:
        # A transaction-level advisory lock - released with the transaction, or the connection if
        # the process dies.
        # Local import: the constants module instantiates this class.
        from hare.dialects.postgresql.schema.constants import POSTGRESQL_MIGRATION_LOCK_KEY

        return f"SELECT pg_advisory_xact_lock({POSTGRESQL_MIGRATION_LOCK_KEY})"
