from __future__ import annotations

from hare.dialects.base.schema.schema_editor_part import SchemaEditorPart


class TableLocks(SchemaEditorPart):
    """Locks taken while the schema changes: a table locked for a migration step, and the lock that
    keeps two migration runs apart."""

    __slots__ = ()

    @classmethod
    def get_lock_table_sql(cls, qualified_table: str) -> str | None:
        """Returns the statement locking a table against concurrent writes until the end of the
        transaction, while still letting it be read.

        Args:
            qualified_table: The quoted, schema-qualified table.

        Returns:
            The statement, None where a transaction's own writes already keep other writers out.
        """
        return None

    @classmethod
    def get_migration_lock_sql(cls) -> str | None:
        """Returns the statement taking hare's migration lock inside a transaction, held until the
        transaction ends - ``migrate`` holds it on a connection of its own for the whole run, so two
        processes migrating one database at once apply their migrations one after the other.

        Returns:
            The statement, None where the database has no such lock.
        """
        return None
