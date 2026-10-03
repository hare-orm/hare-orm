from datetime import UTC, datetime

from hare import fields
from hare.core.log import logger
from hare.ddl.indexes.index import Index
from hare.migrations.loading.graph.migration_key import MigrationKey
from hare.models import Model
from hare.sql import Table


class MigrationRecorder:
    def __init__(self, connection, *, table_name: str = "hare_migrations") -> None:
        self.connection = connection
        self.table_name = table_name
        self.model = self._make_model(table_name)

    def _get_table(self) -> Table:
        """The recorder's table, for a statement built with the connection's query class."""
        return Table(self.table_name)

    def _make_model(self, table_name: str) -> type[Model]:
        class MigrationRecord(Model):
            id = fields.IntField(primary_key=True)
            app = fields.CharField(max_length=255)
            name = fields.CharField(max_length=255)
            applied_at = fields.DatetimeField()

            class Meta:
                table = table_name
                app = "_migrations"
                indexes = (Index(fields=("app", "name"), unique=True),)

        return MigrationRecord

    async def ensure_schema(self, schema_editor) -> None:
        await schema_editor.client.execute_script(schema_editor._get_model_sql_data(self.model, safe=True).table_sql)

    async def _table_exists(self) -> bool:
        """Whether the journal table exists - checked by name: a failed SELECT can be any transient
        failure, and taking it for a missing table would apply migrations again.
        """
        # Local import: the introspector's modules import the migrations package.
        from hare.inspectdb.introspector.schema_introspector import SchemaIntrospector

        return await SchemaIntrospector.table_exists(self.connection, self.table_name)

    async def applied_migrations(self) -> list[MigrationKey]:
        if not await self._table_exists():
            # Expected on a fresh database before ensure_schema() has ever run -
            # treat as "no migrations applied yet" instead of raising.
            logger.debug("Migrations table %r not found yet", self.table_name)
            return []
        table = self._get_table()
        query = (
            self.connection.query_class.from_(table)
            .select(table.app, table.name)
            .orderby(table.applied_at, table.app, table.name)
        )
        _, rows = await self.connection.execute(*query.get_parameterized_sql())
        return [MigrationKey(app_label=row["app"], name=row["name"]) for row in rows]

    async def record_applied(self, app: str, name: str, connection=None) -> None:
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
        applied_at = datetime.now(UTC)
        query = (
            self.connection.query_class.into(self._get_table())
            .columns("app", "name", "applied_at")
            .insert(app, name, applied_at)
        )
        await (connection or self.connection).execute(*query.get_parameterized_sql())

    async def record_unapplied(self, app: str, name: str, connection=None) -> None:
        """Records a migration as unapplied.

        Args:
            app: App label the migration belongs to.
            name: Migration name.
            connection: Same override as ``record_applied``.
        """
        table = self._get_table()
        query = self.connection.query_class.from_(table).where((table.app == app) & (table.name == name)).delete()
        await (connection or self.connection).execute(*query.get_parameterized_sql())
