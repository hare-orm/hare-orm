from __future__ import annotations

from collections.abc import Sequence
from contextlib import AbstractAsyncContextManager

from hare.dialects.base.client.database_client import DatabaseClient
from hare.dialects.base.schema.schema_editor_part import SchemaEditorPart
from hare.exceptions import UnSupportedError
from hare.models import Model


class TableClearing(SchemaEditorPart):
    """Tables emptied at run time - every row deleted, the cascading foreign keys deferred while it
    happens."""

    __slots__ = ()

    @classmethod
    async def clear_tables(cls, connection: DatabaseClient, quoted_tables: Sequence[str]) -> None:
        """Deletes every row of the tables - a test database is reset between tests with it.

        Args:
            connection: The connection the tables are on.
            quoted_tables: The quoted, schema-qualified where the dialect has schemas, table names,
                a table referencing another before it.
        """
        if quoted_tables:
            # One script - a statement per table would be a round trip per table.
            script = "".join(f"DELETE FROM {quoted_table};\n" for quoted_table in quoted_tables)  # nosec
            await connection.execute_script(script)

    @classmethod
    def defer_cascade_foreign_keys(
        cls, model: type[Model], connection: DatabaseClient
    ) -> AbstractAsyncContextManager[bool]:
        """Defers, for the block, the foreign keys a hard delete of ``model`` rows made of several
        statements (or of one, see ``checks_foreign_keys_per_cascade_step``) can run into, so a row
        guarded by an ``on_delete=PROTECT`` relation doesn't fail the delete when the same cascade
        removes the guarding row too. The deferred constraints are checked again when the block
        ends.

        Args:
            model: The model rows are deleted from.
            connection: A client inside the transaction the delete runs in.

        Returns:
            An async context manager yielding whether anything was deferred.

        Raises:
            UnSupportedError: The dialect has no way to defer them.
        """
        raise UnSupportedError(
            f"on_delete=PROTECT inside a cascade has no deferral on the {connection.dialect} dialect"
        )
