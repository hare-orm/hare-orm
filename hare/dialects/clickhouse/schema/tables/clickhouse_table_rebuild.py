from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from hare.dialects.base.schema.plans.column_copy import ColumnCopy
from hare.dialects.base.schema.plans.enums import SchemaEffect
from hare.dialects.base.schema.plans.schema_change_plan import SchemaChangePlan
from hare.dialects.base.schema.plans.schema_cleanup import SchemaCleanup
from hare.dialects.base.schema.plans.schema_statement import SchemaStatement
from hare.dialects.base.schema.tables.table_rebuild import TableRebuild
from hare.dialects.clickhouse.clickhouse_table_options import ClickhouseTableOptions
from hare.dialects.clickhouse.client.constants import CLICKHOUSE_DEFAULT_DATABASE
from hare.dialects.clickhouse.cluster.clickhouse_placement_plan import ClickhousePlacementPlan
from hare.dialects.clickhouse.cluster.constants import (
    CLICKHOUSE_CLUSTER_HOSTS_SQL,
    CLICKHOUSE_DATABASE_ENGINE_SQL,
    CLICKHOUSE_DISTRIBUTED_TABLE_TEMPLATE,
    CLICKHOUSE_SHARD_MACRO,
    CLICKHOUSE_UUID_MACRO,
)
from hare.dialects.clickhouse.introspection.clickhouse_catalog_engine import ClickhouseCatalogEngine
from hare.dialects.clickhouse.introspection.clickhouse_sql_parts import ClickhouseSqlParts
from hare.dialects.clickhouse.introspection.constants import (
    CLICKHOUSE_DISTRIBUTED_ENGINE_NAME,
    CLICKHOUSE_ENGINE_CLAUSES,
    CLICKHOUSE_REPLICA_DEFAULTS_SQL,
    CLICKHOUSE_TABLE_ENGINE_SQL,
)
from hare.dialects.clickhouse.schema.constants import (
    CLICKHOUSE_EXCHANGE_DATABASE_ENGINES,
    CLICKHOUSE_EXCHANGE_TABLES_TEMPLATE,
    CLICKHOUSE_FLUSH_DISTRIBUTED_TEMPLATE,
    CLICKHOUSE_REBUILD_TARGET_SQL,
)
from hare.dialects.clickhouse.schema.messages import (
    ENGINE_FIXED_DEFAULT_REPLICA_PATH,
    ENGINE_RAW_REBUILD_PATH,
    REBUILD_DATABASE_ENGINE_UNSUPPORTED,
    REBUILD_STORAGE_TARGET_EXISTS,
    REBUILD_STORAGE_TO_ROUTER,
)
from hare.dialects.clickhouse.schema.tables.clickhouse_rebuild_check import ClickhouseRebuildCheck
from hare.exceptions import ConfigurationError, UnSupportedError
from hare.fields.field import Field
from hare.fields.relations.fields.foreign_key_field_instance import ForeignKeyFieldInstance
from hare.models import Model
from hare.sql.identifiers import Identifiers

if TYPE_CHECKING:
    from hare.dialects.base.schema.data.referencing_key_columns import ReferencingKeyColumns
    from hare.dialects.clickhouse.schema.clickhouse_schema_editor import ClickhouseSchemaEditor


class ClickhouseTableRebuild(TableRebuild):
    """Copy rows into a staging table before dropping views and dictionaries for the exchange.

    A failed copy leaves the original table and its dependencies in place. Views and dictionaries
    are recreated over the replacement before the old table is removed. Distributed rows are copied
    through the cluster, each to its shard; the exchange is not atomic across servers.
    """

    __slots__ = ()

    editor: ClickhouseSchemaEditor

    def get_rebuild_plan(
        self,
        old_model: type[Model],
        new_model: type[Model],
        altered_fields: Sequence[tuple[Field[Any], Field[Any]]] = (),
    ) -> SchemaChangePlan:
        """Prepares the table rebuild and recreation of its views and dictionaries.

        Execution fills a materialized view's storage from its query and preserves rows in a
        table it writes to. Placement and database-engine checks run when the plan executes.

        Args:
            old_model: The model the table matches now - its views and dictionaries are dropped.
            new_model: The model the table is rebuilt to - its views and dictionaries are created.
            altered_fields: (old, new) fields paired otherwise than by name - a renamed field.

        Raises:
            UnSupportedError: Storage would become a distributed router, or the replicated engine
                names a fixed replica path without ``{uuid}``.
        """
        dialect = self.editor.client.dialect
        old_options = ClickhouseTableOptions.get_for_model(old_model, dialect)
        new_options = ClickhouseTableOptions.get_for_model(new_model, dialect)
        if (
            new_options.get_engine_name() == CLICKHOUSE_DISTRIBUTED_ENGINE_NAME
            and old_options.get_engine_name() != CLICKHOUSE_DISTRIBUTED_ENGINE_NAME
        ):
            raise UnSupportedError(REBUILD_STORAGE_TO_ROUTER)
        self.raise_if_replica_path_is_fixed(new_model)
        plan = super().get_rebuild_plan(old_model, new_model, altered_fields)
        return SchemaChangePlan(
            (
                *self.editor.table_creation.get_replica_check_plan(new_model).steps,
                ClickhouseRebuildCheck(old_model, new_model),
                *self.get_distributed_flush_plan(old_model).steps,
                *plan.steps,
            ),
            before=plan.before,
            after=plan.after,
        )

    def get_distributed_flush_plan(self, model: type[Model]) -> SchemaChangePlan:
        """Drain existing routing queues before copying rows or replacing a router.

        Queue delivery is local to each server, even when the database replicates
        DDL. Use the full connection cluster, independently of schema placement.
        Writers must remain stopped throughout the rebuild.
        """
        client = self.editor.client
        options = ClickhouseTableOptions.get_for_model(model, client.dialect)
        if options.distributed_over is None and options.get_engine_name() != CLICKHOUSE_DISTRIBUTED_ENGINE_NAME:
            return SchemaChangePlan(())
        cluster = client.cluster.name if client.cluster is not None else None
        sql = CLICKHOUSE_FLUSH_DISTRIBUTED_TEMPLATE.format(
            table=self.editor.qualify_table_name(
                model._meta.db_table, model._meta.schema or client.database or CLICKHOUSE_DEFAULT_DATABASE
            ),
            on_cluster=ClickhousePlacementPlan.get_cluster_sql(cluster, client.sql_context),
        )
        return SchemaChangePlan((SchemaStatement(sql),))

    @staticmethod
    def get_rebuilt_table_name(table_name: str) -> str:
        """Keep failed or uncertain replacements separate from subsequent attempts."""
        return Identifiers.get_within_limit(f"{table_name} rebuild {uuid4().hex}")

    def get_before_replace_plan(self, old_model: type[Model], new_model: type[Model]) -> SchemaChangePlan:
        """Drop dependencies only after the replacement table's row copy succeeds."""
        old_options = ClickhouseTableOptions.get_for_model(old_model, self.editor.client.dialect)
        companion_drop = (
            (f"DROP TABLE {self.editor.get_schema_table_sql(old_model._meta.db_table, old_model._meta.schema)}",)
            if old_options.distributed_over is not None
            else ()
        )
        return SchemaChangePlan(
            tuple(
                SchemaStatement(sql)
                for sql in (
                    *self.editor.get_model_view_drop_sqls(old_model),
                    *self.editor.dictionaries.get_model_drop_sqls(old_model),
                    *companion_drop,
                )
            )
        )

    def get_replace_plan(
        self,
        table_name: str,
        rebuilt_table_name: str,
        schema: str | None,
        fields: Iterable[Field[Any]],
    ) -> SchemaChangePlan:
        """Exchange table identities without first deleting the original data.

        A failed or unacknowledged exchange leaves both names for inspection. The
        previous table is dropped only after dependent objects are recreated.
        Cluster execution has no cross-host atomicity guarantee.
        """
        return SchemaChangePlan(
            (
                SchemaStatement(
                    CLICKHOUSE_EXCHANGE_TABLES_TEMPLATE.format(
                        table=self.editor.qualify_table_name(table_name, schema),
                        replacement=self.editor.qualify_table_name(rebuilt_table_name, schema),
                        on_cluster=self.editor.get_schema_cluster_sql(schema),
                    ),
                    frozenset({SchemaEffect.REPLACE_TABLE}),
                ),
            )
        )

    def get_rebuild_finalization_plan(
        self,
        old_model: type[Model],
        new_model: type[Model],
        rebuilt_table_name: str,
    ) -> SchemaChangePlan:
        """Restore dependent objects before releasing the previous table's contents."""
        sqls = [
            *(
                sql
                for view in new_model._meta.views
                for sql in self.editor.views.get_view_create_sqls(new_model, view)
            ),
            *(
                sql
                for dictionary in new_model._meta.dictionaries
                for sql in self.editor.dictionaries.get_dictionary_create_sqls(new_model, dictionary)
            ),
            *(
                sql
                for view in new_model._meta.materialized_views
                for sql in self.editor.materialized_views.get_recreation_sqls(new_model, view)
            ),
            f"DROP TABLE {self.editor.get_schema_table_sql(rebuilt_table_name, new_model._meta.schema)}",
        ]
        return SchemaChangePlan(tuple(SchemaStatement(sql) for sql in sqls))

    async def check_database_exchange(self, model: type[Model]) -> None:
        """Reject unsupported database engines before creating or dropping anything."""
        database = model._meta.schema or self.editor.client.database or CLICKHOUSE_DEFAULT_DATABASE
        engine = self.editor.get_database_engine(database)
        if engine is None:
            rows = await self.editor.client.execute_dicts(CLICKHOUSE_DATABASE_ENGINE_SQL, [database])
            engine = str(rows[0]["engine"]) if rows else None
        if engine not in CLICKHOUSE_EXCHANGE_DATABASE_ENGINES:
            raise UnSupportedError(REBUILD_DATABASE_ENGINE_UNSUPPORTED.format(database=database, engine=engine))

    async def check_storage_target(self, old_model: type[Model], new_model: type[Model]) -> None:
        """Refuse an unrelated existing destination before copying or removing dependencies."""
        old_storage = self.get_storage_table_name(old_model)
        new_storage = self.get_storage_table_name(new_model)
        if old_storage == new_storage:
            return
        old_options = ClickhouseTableOptions.get_for_model(old_model, self.editor.client.dialect)
        if old_options.distributed_over is not None and new_storage == old_model._meta.db_table:
            return  # This declared companion is explicitly dropped before replacement.
        database = new_model._meta.schema or self.editor.client.database or CLICKHOUSE_DEFAULT_DATABASE
        if await self.editor.client.execute_dicts(CLICKHOUSE_REBUILD_TARGET_SQL, [database, new_storage]):
            raise ConfigurationError(REBUILD_STORAGE_TARGET_EXISTS.format(database=database, table=new_storage))

    def get_referencing_key_rebuild_plan(self, referencing: ReferencingKeyColumns) -> SchemaChangePlan | None:
        """Prepares a rebuild for a relation whose key columns are keys of the table - the server changes
        no key column in place.

        Args:
            referencing: The relation key columns, with their new types.

        Returns:
            The prepared rebuild plan, or None when no rebuild is needed.
        """
        if not isinstance(referencing.relation_field, ForeignKeyFieldInstance) or not any(
            self.is_key_column(referencing.model, column) for column, __ in referencing.key_columns
        ):
            return None
        return self.get_rebuild_plan(referencing.model, referencing.model)

    def get_column_change_rebuild_plan(
        self, model: type[Model], old_field: Field[Any], new_field: Field[Any]
    ) -> SchemaChangePlan | None:
        """Prepares a rebuild for a new type or nullability of a key column - the server changes
        no key column in place.

        Args:
            model: The model rendered from the state the table changes to.
            old_field: The field before the change.
            new_field: The field after it.

        Returns:
            The prepared rebuild plan, or None when no rebuild is needed.
        """
        dialect = self.editor.client.dialect
        if (
            old_field.get_column_type(dialect) == new_field.get_column_type(dialect)
            and old_field.null == new_field.null
        ):
            return None
        if not self.is_key_column(model, new_field.source_field or new_field.model_field_name):
            return None
        return self.get_rebuild_plan(model, model, altered_fields=[(old_field, new_field)])

    def is_key_column(self, model: type[Model], column_name: str) -> bool:
        """Whether a column is one of the table's keys.

        Args:
            model: The model of the table.
            column_name: The column.

        Returns:
            True for a column of the primary key or read by a key expression.
        """
        meta = model._meta
        if column_name in meta.get_column_names(meta.primary_key_attribute_names):
            return True
        dialect = self.editor.client.dialect
        table_options = meta.get_table_options(dialect)
        if not isinstance(table_options, ClickhouseTableOptions):
            return False
        return column_name in table_options.get_key_column_names(model, dialect)

    def is_read_by_schema_objects(self, model: type[Model], field_name: str, column_name: str) -> bool:
        """Whether a column is read by what the table's model declares over it - a projection, a
        materialized view, a dictionary: the server renames no column one of them reads.

        Args:
            model: The model of the table.
            field_name: The column's field.
            column_name: The column.

        Returns:
            True for a column a projection's query or a dictionary reads, and for any column of a
            table with a materialized view.
        """
        meta = model._meta
        if meta.materialized_views:
            return True
        if any(field_name in dictionary.get_field_names() for dictionary in meta.dictionaries):
            return True
        table_options = meta.get_table_options(self.editor.client.dialect)
        if not isinstance(table_options, ClickhouseTableOptions):
            return False
        # A raw SQL projection's columns are the ones it names in read_columns.
        return any(column_name in projection.get_read_columns() for projection in table_options.projections)

    def raise_if_replica_path_is_fixed(self, model: type[Model]) -> None:
        """Refuses to remake a replicated table whose replica path names no ``{uuid}`` - the new table,
        created beside the old one, took the old one's path (the server refused it), or a path naming the new
        table's name it kept once renamed.

        Args:
            model: The model.

        Raises:
            UnSupportedError: The table's replicated engine names a replica path without ``{uuid}``.
        """
        engine = ClickhouseTableOptions.get_for_model(model, self.editor.client.dialect).get_engine()
        if not engine.is_replicated():
            return
        if engine.sql is not None:
            raise UnSupportedError(ENGINE_RAW_REBUILD_PATH)
        if engine.replica_path is None:
            return
        if CLICKHOUSE_UUID_MACRO not in engine.replica_path:
            raise UnSupportedError(
                f"{model.__name__}: the change remakes the table, whose replica path names no "
                f"{CLICKHOUSE_UUID_MACRO} - the new table would take the path of the old one. Declare the engine "
                f"without its path (the server's default_replica_path), or with a path naming {CLICKHOUSE_UUID_MACRO}"
            )

    async def raise_if_default_replica_path_is_fixed(self, model: type[Model]) -> str | None:
        """Validate and return the new table's explicit or server-default replica path."""
        engine = ClickhouseTableOptions.get_for_model(model, self.editor.client.dialect).get_engine()
        if not engine.is_replicated() or engine.replica_path is not None:
            return engine.replica_path
        defaults = {
            str(row["name"]): str(row["value"])
            for row in await self.editor.client.execute_dicts(CLICKHOUSE_REPLICA_DEFAULTS_SQL)
        }
        if CLICKHOUSE_UUID_MACRO not in defaults.get("default_replica_path", ""):
            raise UnSupportedError(
                ENGINE_FIXED_DEFAULT_REPLICA_PATH.format(model=model.__name__, uuid=CLICKHOUSE_UUID_MACRO)
            )
        return defaults["default_replica_path"]

    async def raise_if_rows_are_scattered(self, model: type[Model], *, replica_path: str | None = None) -> None:
        """Refuses to remake a table whose rows the connection's server holds only a part of.

        Args:
            model: The model.
            replica_path: The new table's replica path, or None to inspect the existing table.

        Raises:
            UnSupportedError: The table isn't distributed, and the connection's cluster has several servers
                keeping rows of their own - several servers for a table of another engine, several shards
                for a replicated one whose replica path names the shard: the rows of the others would be
                lost, or copied into every shard.
        """
        client = self.editor.client
        if client.cluster is None or self.editor.collect_sql:
            return
        options = ClickhouseTableOptions.get_for_model(model, client.dialect)
        if options.distributed_over is not None or options.get_engine_name() == CLICKHOUSE_DISTRIBUTED_ENGINE_NAME:
            return
        rows = await client.execute_dicts(CLICKHOUSE_CLUSTER_HOSTS_SQL, [client.cluster.name])
        if not rows:
            return
        if options.get_engine().is_replicated():
            if int(rows[0]["shards"]) > 1 and await self.is_replicated_per_shard(model, replica_path=replica_path):
                raise UnSupportedError(
                    f"{model.__name__}: the change remakes the table, whose replicas keep rows of their own on each "
                    f"shard of the cluster {client.cluster.name!r} - the copy would read the rows of one shard. "
                    "Distribute the table (ClickhouseTableOptions(distributed_over=...))"
                )
        elif int(rows[0]["hosts"]) > 1:
            raise UnSupportedError(
                f"{model.__name__}: the change remakes the table, which keeps rows of its own on each server of the "
                f"cluster {client.cluster.name!r} - only the rows of this server would be copied. Store the table "
                "by a Replicated engine, or distribute it (ClickhouseTableOptions(distributed_over=...))"
            )

    async def is_replicated_per_shard(self, model: type[Model], *, replica_path: str | None = None) -> bool:
        """Whether the replicas of a replicated table are those of one shard - its replica path names the
        shard (``{shard}``, as the server's default path does) - rather than every server of the cluster.

        Args:
            model: The model.
            replica_path: A prepared destination path; None reads the source's catalog engine.

        Returns:
            Whether they are.
        """
        if replica_path is not None:
            return CLICKHOUSE_SHARD_MACRO in replica_path
        client = self.editor.client
        meta = model._meta
        rows = await client.execute_dicts(
            CLICKHOUSE_TABLE_ENGINE_SQL, [meta.schema or client.database or CLICKHOUSE_DEFAULT_DATABASE, meta.db_table]
        )
        if not rows:
            return True
        clauses = ClickhouseSqlParts.get_clauses(str(rows[0]["engine_full"]), CLICKHOUSE_ENGINE_CLAUSES)
        engine = ClickhouseCatalogEngine.get(clauses[""])
        if isinstance(engine, str) or engine.sql is not None or engine.replica_path is None:
            return True
        return CLICKHOUSE_SHARD_MACRO in engine.replica_path

    def get_copy_source_table(self, model: type[Model]) -> str:
        """The original logical table reads every source shard through its existing engine."""
        return model._meta.db_table

    def get_copy_plan(
        self, model: type[Model], new_table_name: str, source_sql: str, columns: tuple[ColumnCopy, ...]
    ) -> SchemaChangePlan:
        """Prepare a distributed copy with a private temporary table and guaranteed cleanup.

        CREATE never replaces an existing table. Cleanup is entered only after the
        temporary table was successfully created, and completes before a failed or
        cancelled copy returns. The generated name belongs to this plan alone.
        """
        client = self.editor.client
        options = ClickhouseTableOptions.get_for_model(model, client.dialect)
        if options.get_engine_name() == CLICKHOUSE_DISTRIBUTED_ENGINE_NAME:
            # The replacement routes to existing storage; copying would insert its rows again.
            return SchemaChangePlan(())
        if options.distributed_over is None or client.cluster is None:
            copy_plan = super().get_copy_plan(model, new_table_name, source_sql, columns)
        else:
            meta = model._meta
            distributed_copy_name = Identifiers.get_within_limit(f"{meta.db_table} copy {uuid4().hex}")
            distributed_copy_sql = self.editor.get_schema_table_sql(distributed_copy_name, meta.schema)
            create_sql = CLICKHOUSE_DISTRIBUTED_TABLE_TEMPLATE.format(
                or_replace="",
                exists="",
                table=distributed_copy_sql,
                local_table=self.editor.qualify_table_name(new_table_name, meta.schema),
                engine=options.get_distributed_engine_sql(model, self.editor, client.cluster.name, new_table_name),
            )
            copy_plan = SchemaChangePlan(
                (
                    SchemaStatement(create_sql),
                    SchemaCleanup(
                        super().get_copy_plan(model, distributed_copy_name, source_sql, columns),
                        SchemaChangePlan((SchemaStatement(f"DROP TABLE {distributed_copy_sql}"),)),
                    ),
                )
            )
        return copy_plan

    def get_staging_plan(
        self,
        model: type[Model],
        rebuilt_table_name: str,
        preparation: SchemaChangePlan,
    ) -> SchemaChangePlan:
        """Release the acquired staging table if its setup or row copy fails.

        This boundary ends before dependencies are dropped or names exchanged, so
        cleanup never guesses which table a failed exchange left under each name.
        """
        replacement_sql = self.editor.get_schema_table_sql(rebuilt_table_name, model._meta.schema)
        return SchemaChangePlan(
            (
                SchemaCleanup(
                    preparation,
                    SchemaChangePlan((SchemaStatement(f"DROP TABLE {replacement_sql}"),)),
                    release_on_success=False,
                ),
            )
        )
