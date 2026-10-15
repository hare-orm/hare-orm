from __future__ import annotations

from typing import TYPE_CHECKING

from hare import fields
from hare.core.log import logger
from hare.migrations.loading.graph.migration_key import MigrationKey
from hare.models import Model
from hare.sql import Table
from hare.time.system_clock import SystemClock

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.dialects.base.schema.base_schema_editor import BaseSchemaEditor


class MigrationRecorder:
    """The journal of the applied migrations, kept in a table of the database."""

    def __init__(self, connection: DatabaseClient, *, table_name: str = "hare_migrations") -> None:
        self.connection = connection
        self.table_name = table_name

    def _get_table(self) -> Table:
        """The recorder's table, for a statement built with the connection's query class."""
        return Table(self.table_name)

    def get_model(self, connection: DatabaseClient) -> type[Model]:
        """The model of the journal's table, as a connection stores it.

        Args:
            connection: The connection the table is created on.

        Returns:
            The model.
        """
        table_name = self.table_name
        journal_table_options = connection.dialect.get_journal_table_options(connection)

        # Keyed by the migration itself, not by a generated key - the journal is written on a
        # database that generates no keys too.
        class MigrationRecord(Model):
            app = fields.CharField(max_length=255)
            name = fields.CharField(max_length=255)
            applied_at = fields.DatetimeField()
            pk = fields.CompositePrimaryKey("app", "name")

            class Meta:
                table = table_name
                app = "_migrations"
                table_options = [journal_table_options] if journal_table_options is not None else []

        return MigrationRecord

    async def ensure_schema(self, schema_editor: BaseSchemaEditor) -> None:
        await schema_editor.client.execute_script(
            schema_editor.table_creation.get_model_sql_data(self.get_model(schema_editor.client), safe=True).table_sql
        )

    async def _table_exists(self, connection: DatabaseClient | None = None) -> bool:
        """Whether the journal table exists - checked by name: a failed SELECT can be any transient
        failure, and taking it for a missing table would apply migrations again.

        Args:
            connection: Overrides ``self.connection`` for this one call.
        """
        # Local import: the introspector's modules import the migrations package.
        from hare.inspectdb.introspection.database_catalog import DatabaseCatalog

        return await DatabaseCatalog.table_exists(connection or self.connection, self.table_name)

    async def applied_migrations(self, connection: DatabaseClient | None = None) -> list[MigrationKey]:
        """The migrations recorded as applied.

        Args:
            connection: Overrides ``self.connection`` for this one call - a migration's own
                transaction reads what it holds, and doesn't wait for itself on a connection it
                keeps busy.

        Returns:
            Their keys, in the order they were applied.
        """
        if not await self._table_exists(connection):
            # Expected on a fresh database before ensure_schema() has ever run -
            # treat as "no migrations applied yet" instead of raising.
            logger.debug("Migrations table %r not found yet", self.table_name)
            return []
        # A migration another server of the database applied is read too.
        reading_connection = connection or self.connection
        await reading_connection.dialect.synchronize_table(reading_connection, self.table_name)
        table = self._get_table()
        query = (
            self.connection.query_class.from_(table)
            .select(table.app, table.name)
            .orderby(table.applied_at, table.app, table.name)
        )
        _, rows = await (connection or self.connection).execute(*query.get_parameterized_sql())
        return [MigrationKey(app_label=row["app"], name=row["name"]) for row in rows]

    async def record_applied(self, app: str, name: str, connection: DatabaseClient | None = None) -> None:
        """Records a migration as applied.

        Args:
            app: App label the migration belongs to.
            name: Migration name.
            connection: Overrides ``self.connection`` for this one call - lets a caller record
                the migration as applied on the SAME transaction/connection its DDL just ran on,
                instead of always going through this recorder's own connection, a separate call
                that could commit independently of (and after) the DDL transaction it's meant to
                be atomic with.
        """
        applied_at = SystemClock.get_utc_now()
        query = (
            self.connection.query_class.into(self._get_table())
            .columns("app", "name", "applied_at")
            .insert(app, name, applied_at)
        )
        await (connection or self.connection).execute(*query.get_parameterized_sql())

    async def record_unapplied(self, app: str, name: str, connection: DatabaseClient | None = None) -> None:
        """Records a migration as unapplied.

        Args:
            app: App label the migration belongs to.
            name: Migration name.
            connection: Same override as ``record_applied``.
        """
        table = self._get_table()
        query = self.connection.query_class.from_(table).where((table.app == app) & (table.name == name)).delete()
        await (connection or self.connection).execute(*query.get_parameterized_sql())
