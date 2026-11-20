from __future__ import annotations

from typing import TYPE_CHECKING

from hare import fields
from hare.core.log import logger
from hare.migrations.constants import DEFAULT_MIGRATION_TABLE_NAME
from hare.migrations.loading.graph.migration_key import MigrationKey
from hare.models import Model
from hare.sql import Table
from hare.time.system_clock import SystemClock

if TYPE_CHECKING:
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.dialects.base.schema.base_schema_editor import BaseSchemaEditor
    from hare.models.model_binding import ModelBinding


class MigrationRecorder:
    """The journal of the applied migrations, kept in a table of the database."""

    def __init__(self, connection: DatabaseClient, *, table_name: str = DEFAULT_MIGRATION_TABLE_NAME) -> None:
        self.connection = connection
        self.table_name = table_name

    def _get_table(self) -> Table:
        """The recorder's table, for a statement built with the connection's query class."""
        return Table(self.table_name)

    def get_binding(self, connection: DatabaseClient) -> ModelBinding:
        """Prepare the journal's structural metadata using the target connection's dialect.

        Args:
            connection: The connection the table is created on.

        Returns:
            The prepared journal binding, without retaining the connection.
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

        from hare.core.apps import Apps
        from hare.core.apps.model_binding_preparation import ModelBindingPreparation
        from hare.core.connections.connection_handler import ConnectionHandler

        apps = Apps({}, ConnectionHandler(), validate_connections=False, dialects={"_migrations": connection.dialect})
        with ModelBindingPreparation(apps, (MigrationRecord,)):
            apps.apps["_migrations"] = {MigrationRecord.__name__: MigrationRecord}
            binding = apps.get_binding(MigrationRecord)
            binding.default_connection = "_migrations"
            apps.init_relations()
            apps.bind_model_base_queries(binding, connection)
        return binding

    async def ensure_schema(self, schema_editor: BaseSchemaEditor) -> None:
        await schema_editor.client.execute_built_script(
            schema_editor.table_creation.get_model_sql_data(
                self.get_binding(schema_editor.client), safe=True
            ).table_sql
        )

    async def applied_migrations(self, connection: DatabaseClient | None = None) -> list[MigrationKey]:
        """The migrations recorded as applied.

        Args:
            connection: Overrides ``self.connection`` for this one call - a migration's own
                transaction reads what it holds, and doesn't wait for itself on a connection it
                keeps busy.

        Returns:
            Their keys, in the order they were applied.
        """
        # Local import: the introspector's modules import the migrations package.
        from hare.inspectdb.introspection.database_catalog import DatabaseCatalog

        reading_connection = connection or self.connection
        if not await DatabaseCatalog.table_exists(reading_connection, self.table_name):
            # Expected on a fresh database before ensure_schema() has ever run -
            # treat as "no migrations applied yet" instead of raising.
            logger.debug("Migrations table %r not found yet", self.table_name)
            return []
        # A migration another server of the database applied is read too.
        await reading_connection.dialect.synchronize_table(reading_connection, self.table_name)
        table = self._get_table()
        query = (
            self.connection.query_class.from_(table)
            .select(table.app, table.name)
            .orderby(table.applied_at, table.app, table.name)
        )
        _, rows = await reading_connection._execute_built(*query.get_parameterized_sql())
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
        await (connection or self.connection)._execute_built(*query.get_parameterized_sql())

    async def record_unapplied(self, app: str, name: str, connection: DatabaseClient | None = None) -> None:
        """Records a migration as unapplied.

        Args:
            app: App label the migration belongs to.
            name: Migration name.
            connection: Same override as ``record_applied``.
        """
        table = self._get_table()
        query = self.connection.query_class.from_(table).where((table.app == app) & (table.name == name)).delete()
        await (connection or self.connection)._execute_built(*query.get_parameterized_sql())
