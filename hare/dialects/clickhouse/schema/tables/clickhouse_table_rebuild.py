from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from hare.dialects.base.schema.tables.table_rebuild import TableRebuild
from hare.dialects.clickhouse.clickhouse_table_options import ClickhouseTableOptions
from hare.dialects.clickhouse.cluster.constants import (
    CLICKHOUSE_CLUSTER_HOSTS_SQL,
    CLICKHOUSE_DISTRIBUTED_ENGINE_TEMPLATE,
    CLICKHOUSE_DISTRIBUTED_TABLE_TEMPLATE,
    CLICKHOUSE_REPLICATED_ENGINE_PREFIX,
)
from hare.exceptions import UnSupportedError
from hare.fields.field import Field
from hare.fields.relations.fields.foreign_key_field_instance import ForeignKeyFieldInstance
from hare.models import Model

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.schema.data.referencing_key_columns import ReferencingKeyColumns
    from hare.dialects.clickhouse.schema.declarations import ClickhouseSchemaEditor


class ClickhouseTableRebuild(TableRebuild):
    """TableRebuild as ClickHouse does it - the views and the dictionaries a model declares are dropped
    before its table is remade and created again over the new one: a view follows the inserts of the
    table it was created over, and would take the copied rows a second time; the server drops no table
    a dictionary reads. The rows of a table distributed over a cluster are copied through it, each to
    its shard."""

    __slots__ = ()

    editor: ClickhouseSchemaEditor

    async def remake_table(self, model: type[Model], *args: Any, **kwargs: Any) -> None:
        """Rebuilds the table, the model's views and dictionaries created again over it - a
        materialized view's own storage filled by its query, a table one writes to left with its rows.

        Args:
            model: The model rendered from the state the table is rebuilt to.
            *args: The rebuild's other arguments.
            **kwargs: The rebuild's other keyword arguments.

        Raises:
            UnSupportedError: The table keeps rows of its own on several servers of the connection's
                cluster - neither replicated nor distributed.
        """
        await self.raise_if_rows_are_scattered(model)
        meta = model._meta
        materialized_views = self.editor.materialized_views
        await materialized_views.drop_model_materialized_views(model)
        await self.editor.dictionaries.drop_model_dictionaries(model)
        await self.editor.views.drop_model_views(model)
        await super().remake_table(model, *args, **kwargs)
        for view in meta.views:
            await self.editor.views.create_view(model, view)
        for dictionary in meta.dictionaries:
            await self.editor.dictionaries.create_dictionary(model, dictionary)
        for materialized_view in meta.materialized_views:
            await materialized_views.create_materialized_view_again(model, materialized_view)

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
        await self.remake_table(referencing.model)
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
        await self.remake_table(model, alter_fields=[(old_field, new_field)])
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
        table_options = meta.get_table_options(self.editor.client.dialect)
        if not isinstance(table_options, ClickhouseTableOptions):
            return False
        key_sqls = [
            str(getattr(key, "sql", key))
            for key in (
                *table_options.order_by,
                table_options.sample_by,
                table_options.partition_by,
                table_options.sharding_key,
            )
            if key is not None
        ]
        return self.reads_column(key_sqls, column_name)

    def is_read_by_schema_objects(self, model: type[Model], field_name: str, column_name: str) -> bool:
        """Whether a column is read by what the table's model declares over it - a projection, a
        materialized view, a dictionary: the server renames no column one of them reads.

        Args:
            model: The model of the table.
            field_name: The column's field.
            column_name: The column.

        Returns:
            True for a column a projection's query or a dictionary names, and for any column of a
            table with a materialized view or a projection of ``SELECT *``.
        """
        meta = model._meta
        if meta.materialized_views:
            return True
        if any(field_name in dictionary.get_field_names() for dictionary in meta.dictionaries):
            return True
        table_options = meta.get_table_options(self.editor.client.dialect)
        if not isinstance(table_options, ClickhouseTableOptions):
            return False
        projection_sqls = [projection.query.sql for projection in table_options.projections]
        # A projection of SELECT * reads every column.
        return any("*" in sql for sql in projection_sqls) or self.reads_column(projection_sqls, column_name)

    @staticmethod
    def reads_column(sqls: list[str], column_name: str) -> bool:
        """Whether SQL names a column, as an identifier of its own.

        Args:
            sqls: The SQL texts.
            column_name: The column.

        Returns:
            Whether one of them does.
        """
        column_pattern = re.compile(rf"(?<![\w`]){re.escape(column_name)}(?![\w`])|`{re.escape(column_name)}`")
        return any(column_pattern.search(sql) for sql in sqls)

    async def raise_if_rows_are_scattered(self, model: type[Model]) -> None:
        """Refuses to remake a table whose rows the connection's server holds only a part of.

        Args:
            model: The model.

        Raises:
            UnSupportedError: The connection's cluster has several servers, and the table is neither
                replicated nor distributed - the rows of the other servers would be lost.
        """
        client = self.editor.client
        if client.cluster is None or self.editor.collect_sql:
            return
        options = ClickhouseTableOptions.get_for_model(model, client.dialect)
        if options.distributed_over is not None or options.engine.startswith(CLICKHOUSE_REPLICATED_ENGINE_PREFIX):
            return
        rows = await client.execute_dicts(CLICKHOUSE_CLUSTER_HOSTS_SQL, [client.cluster.name])
        if rows and int(rows[0]["hosts"]) > 1:
            raise UnSupportedError(
                f"{model.__name__}: the change remakes the table, which keeps rows of its own on each server of the "
                f"cluster {client.cluster.name!r} - only the rows of this server would be copied. Store the table "
                "by a Replicated engine, or distribute it (ClickhouseTableOptions(distributed_over=...))"
            )

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
        literals = client.dialect.literals
        distributed_copy_name = f"new__{meta.db_table}"
        distributed_copy_sql = self.editor.qualify_table_name(distributed_copy_name, meta.schema)
        await self.editor.run_sql(
            CLICKHOUSE_DISTRIBUTED_TABLE_TEMPLATE.format(
                or_replace="OR REPLACE ",
                exists="",
                table=distributed_copy_sql,
                local_table=self.editor.qualify_table_name(new_table_name, meta.schema),
                engine=CLICKHOUSE_DISTRIBUTED_ENGINE_TEMPLATE.format(
                    cluster=client.cluster.name,
                    table=literals.get_string_literal_sql(new_table_name),
                    sharding_key="" if options.sharding_key is None else f", {options.sharding_key.sql}",
                ),
            )
        )
        await super().copy_table_rows(model, distributed_copy_name, meta.db_table, column_mapping)
        await self.editor.run_sql(f"DROP TABLE {distributed_copy_sql}")
