from __future__ import annotations

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
from hare.models import Model

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.clickhouse.schema.declarations import ClickhouseSchemaEditor


class ClickhouseTableRebuild(TableRebuild):
    """TableRebuild as ClickHouse does it - the views a model declares are dropped before its table
    is remade and created again over the new one: a view follows the inserts of the table it was
    created over, and would take the copied rows a second time. The rows of a table distributed over a
    cluster are copied through it, each to its shard."""

    __slots__ = ()

    editor: ClickhouseSchemaEditor

    async def remake_table(self, model: type[Model], *args: Any, **kwargs: Any) -> None:
        """Rebuilds the table, the model's views created again over it - a materialized view's own
        storage filled by its query, a table one writes to left with its rows.

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
        await self.editor.views.drop_model_views(model)
        await super().remake_table(model, *args, **kwargs)
        for view in meta.views:
            await self.editor.views.create_view(model, view)
        for materialized_view in meta.materialized_views:
            await materialized_views.create_materialized_view_again(model, materialized_view)

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
