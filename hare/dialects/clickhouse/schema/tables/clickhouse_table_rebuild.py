from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from hare.dialects.base.schema.tables.table_rebuild import TableRebuild
from hare.dialects.clickhouse.clickhouse_table_options import ClickhouseTableOptions
from hare.dialects.clickhouse.client.constants import CLICKHOUSE_DEFAULT_DATABASE
from hare.dialects.clickhouse.cluster.constants import (
    CLICKHOUSE_CLUSTER_HOSTS_SQL,
    CLICKHOUSE_DISTRIBUTED_TABLE_TEMPLATE,
    CLICKHOUSE_REPLICATED_ENGINE_PREFIX,
    CLICKHOUSE_SHARD_MACRO,
    CLICKHOUSE_UUID_MACRO,
)
from hare.dialects.clickhouse.introspection.clickhouse_sql_parts import ClickhouseSqlParts
from hare.dialects.clickhouse.introspection.constants import CLICKHOUSE_TABLE_ENGINE_SQL
from hare.dialects.clickhouse.types.clickhouse_type_names import ClickhouseTypeNames
from hare.exceptions import UnSupportedError
from hare.fields.field import Field
from hare.fields.relations.fields.foreign_key_field_instance import ForeignKeyFieldInstance
from hare.models import Model

if TYPE_CHECKING:
    from hare.dialects.base.schema.data.referencing_key_columns import ReferencingKeyColumns
    from hare.dialects.clickhouse.schema.clickhouse_schema_editor import ClickhouseSchemaEditor


class ClickhouseTableRebuild(TableRebuild):
    """TableRebuild as ClickHouse does it - the views and the dictionaries a model declares are dropped
    before its table is remade and created again over the new one: a view follows the inserts of the
    table it was created over, and would take the copied rows a second time; the server drops no table
    a dictionary reads. The rows of a table distributed over a cluster are copied through it, each to
    its shard."""

    __slots__ = ()

    editor: ClickhouseSchemaEditor

    async def remake_table(
        self,
        old_model: type[Model],
        new_model: type[Model],
        altered_fields: Sequence[tuple[Field[Any], Field[Any]]] = (),
    ) -> None:
        """Rebuilds the table, the model's views and dictionaries created again over it - a
        materialized view's own storage filled by its query, a table one writes to left with its rows.

        Args:
            old_model: The model the table matches now - its views and dictionaries are dropped.
            new_model: The model the table is rebuilt to - its views and dictionaries are created.
            altered_fields: (old, new) fields paired otherwise than by name - a renamed field.

        Raises:
            UnSupportedError: The table keeps rows of its own on several servers of the connection's
                cluster - neither replicated nor distributed; its replicated engine names a replica path
                without ``{uuid}``.
        """
        self.raise_if_replica_path_is_fixed(new_model)
        await self.raise_if_rows_are_scattered(new_model)
        materialized_views = self.editor.materialized_views
        await materialized_views.drop_model_materialized_views(old_model)
        await self.editor.dictionaries.drop_model_dictionaries(old_model)
        await self.editor.views.drop_model_views(old_model)
        await super().remake_table(old_model, new_model, altered_fields)
        meta = new_model._meta
        for view in meta.views:
            await self.editor.views.create_view(new_model, view)
        for dictionary in meta.dictionaries:
            await self.editor.dictionaries.create_dictionary(new_model, dictionary)
        for materialized_view in meta.materialized_views:
            await materialized_views.create_materialized_view_again(new_model, materialized_view)

    async def remake_for_referencing_key_columns(self, referencing: ReferencingKeyColumns) -> bool:
        """Remakes the table of a relation whose key columns are keys of the table - the server changes
        no key column in place.

        Args:
            referencing: The relation key columns, with their new types.

        Returns:
            Whether the table was remade.
        """
        if not isinstance(referencing.relation_field, ForeignKeyFieldInstance) or not any(
            self.is_key_column(referencing.model, column) for column, __ in referencing.key_columns
        ):
            return False
        await self.remake_table(referencing.model, referencing.model)
        return True

    async def remake_for_column_change(self, model: type[Model], old_field: Field[Any], new_field: Field[Any]) -> bool:
        """Remakes the table for a new type or nullability of a column of its keys - the server changes
        no key column in place.

        Args:
            model: The model rendered from the state the table changes to.
            old_field: The field before the change.
            new_field: The field after it.

        Returns:
            Whether the table was remade.
        """
        dialect = self.editor.client.dialect
        if (
            old_field.get_column_type(dialect) == new_field.get_column_type(dialect)
            and old_field.null == new_field.null
        ):
            return False
        if not self.is_key_column(model, new_field.source_field or new_field.model_field_name):
            return False
        await self.remake_table(model, model, altered_fields=[(old_field, new_field)])
        return True

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
        engine = ClickhouseTableOptions.get_for_model(model, self.editor.client.dialect).engine
        if not engine.startswith(CLICKHOUSE_REPLICATED_ENGINE_PREFIX):
            return
        arguments = ClickhouseTypeNames.get_type_parts(engine)[1]
        if not arguments or not arguments[0].strip().startswith("'"):
            return
        if CLICKHOUSE_UUID_MACRO not in ClickhouseSqlParts.get_string_text(arguments[0].strip()):
            raise UnSupportedError(
                f"{model.__name__}: the change remakes the table, whose replica path names no "
                f"{CLICKHOUSE_UUID_MACRO} - the new table would take the path of the old one. Declare the engine "
                f"without its path (the server's default_replica_path), or with a path naming {CLICKHOUSE_UUID_MACRO}"
            )

    async def raise_if_rows_are_scattered(self, model: type[Model]) -> None:
        """Refuses to remake a table whose rows the connection's server holds only a part of.

        Args:
            model: The model.

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
        if options.distributed_over is not None:
            return
        rows = await client.execute_dicts(CLICKHOUSE_CLUSTER_HOSTS_SQL, [client.cluster.name])
        if not rows:
            return
        if options.engine.startswith(CLICKHOUSE_REPLICATED_ENGINE_PREFIX):
            if int(rows[0]["shards"]) > 1 and await self.is_replicated_per_shard(model):
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

    async def is_replicated_per_shard(self, model: type[Model]) -> bool:
        """Whether the replicas of a replicated table are those of one shard - its replica path names the
        shard (``{shard}``, as the server's default path does) - rather than every server of the cluster.

        Args:
            model: The model.

        Returns:
            Whether they are.
        """
        client = self.editor.client
        meta = model._meta
        rows = await client.execute_dicts(
            CLICKHOUSE_TABLE_ENGINE_SQL, [meta.schema or client.database or CLICKHOUSE_DEFAULT_DATABASE, meta.db_table]
        )
        return not rows or CLICKHOUSE_SHARD_MACRO in str(rows[0]["engine_full"])

    async def copy_table_rows(
        self, model: type[Model], new_table_name: str, old_table_name: str, column_mapping: dict[str, str]
    ) -> None:
        """Copies the rows; those of a distributed table through a distributed table over the new
        local ones - read from every shard, written each to its shard."""
        client = self.editor.client
        options = ClickhouseTableOptions.get_for_model(model, client.dialect)
        if options.distributed_over is None or client.cluster is None:
            await super().copy_table_rows(model, new_table_name, old_table_name, column_mapping)
            return
        meta = model._meta
        distributed_copy_name = f"new__{meta.db_table}"
        distributed_copy_sql = self.editor.qualify_table_name(distributed_copy_name, meta.schema)
        await self.editor.run_sql(
            CLICKHOUSE_DISTRIBUTED_TABLE_TEMPLATE.format(
                or_replace="OR REPLACE ",
                exists="",
                table=distributed_copy_sql,
                local_table=self.editor.qualify_table_name(new_table_name, meta.schema),
                engine=options.get_distributed_engine_sql(model, self.editor, client.cluster.name, new_table_name),
            )
        )
        await super().copy_table_rows(model, distributed_copy_name, meta.db_table, column_mapping)
        await self.editor.run_sql(f"DROP TABLE {distributed_copy_sql}")
