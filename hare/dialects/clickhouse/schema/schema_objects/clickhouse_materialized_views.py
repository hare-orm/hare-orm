from __future__ import annotations

import dataclasses
from typing import Any

from hare.ddl.expressions.declared_expressions import DeclaredExpressions
from hare.ddl.schema_objects.materialized_view import MaterializedView
from hare.dialects.base.schema.schema_objects.materialized_views import MaterializedViews
from hare.dialects.clickhouse.schema.constants import (
    CLICKHOUSE_MATERIALIZED_VIEW_CLUSTER_FILL_MESSAGE,
    CLICKHOUSE_MATERIALIZED_VIEW_CREATE_TEMPLATE,
    CLICKHOUSE_MATERIALIZED_VIEW_FILL_TEMPLATE,
    CLICKHOUSE_MATERIALIZED_VIEW_QUERY_TEMPLATE,
    CLICKHOUSE_MATERIALIZED_VIEW_SCHEDULE_TEMPLATE,
    CLICKHOUSE_MATERIALIZED_VIEW_STORAGE_SETTINGS_SQL,
    CLICKHOUSE_TABLE_TRUNCATE_TEMPLATE,
    CLICKHOUSE_VIEW_DROP_TEMPLATE,
    CLICKHOUSE_VIEW_REFRESH_TEMPLATE,
    CLICKHOUSE_VIEW_WAIT_TEMPLATE,
)
from hare.dialects.clickhouse.schema.schema_objects.clickhouse_views import ClickhouseViews
from hare.dialects.clickhouse.schema_objects.clickhouse_materialized_view import ClickhouseMaterializedView
from hare.exceptions import UnSupportedError
from hare.models import Model


class ClickhouseMaterializedViews(MaterializedViews):
    """MaterializedViews as ClickHouse writes it - a view follows the rows inserted into the table it
    reads, kept in a storage of its own or written to another table, or holds the rows of its query as
    of a refresh on a schedule. It isn't dropped with the table it reads."""

    __slots__ = ()

    def get_materialized_view_create_sqls(
        self, model: type[Model], view: MaterializedView, safe: bool = False
    ) -> list[str]:
        """The statement creating the view - empty: it is filled by the migration adding it."""
        clickhouse_view = ClickhouseMaterializedView.from_materialized_view(view)
        return [
            CLICKHOUSE_MATERIALIZED_VIEW_CREATE_TEMPLATE.format(
                if_not_exists="IF NOT EXISTS " if safe else "",
                view=self.editor.qualify_object_name(model, view.name),
                schedule=self.get_schedule_sql(model, clickhouse_view),
                storage=self.get_storage_sql(model, clickhouse_view),
                # A refreshed view takes its first rows right away unless told to stay empty.
                empty=" EMPTY" if clickhouse_view.refresh is not None and not view.with_data else "",
                query=self.editor.get_view_query_sql(view),
            )
        ]

    def get_schedule_sql(self, model: type[Model], view: ClickhouseMaterializedView) -> str:
        """The refresh schedule of a view as its definition writes it.

        Args:
            model: The model declaring the view.
            view: The view.

        Returns:
            The clause with its leading space; empty for a view without a schedule.
        """
        if view.refresh is None:
            return ""
        schedule_sql = f" REFRESH {view.refresh.strip()}"
        if view.depends_on:
            depended_views = ", ".join(self.editor.qualify_object_name(model, name) for name in view.depends_on)
            schedule_sql += f" DEPENDS ON {depended_views}"
        return schedule_sql + (" APPEND" if view.append else "")

    def get_storage_sql(self, model: type[Model], view: ClickhouseMaterializedView) -> str:
        """Where a view keeps its rows, as its definition writes it.

        Args:
            model: The model declaring the view.
            view: The view.

        Returns:
            The clause with its leading space - the table written to, or the engine and the keys of
            the view's own storage.
        """
        if view.to is not None:
            return f" TO {self.editor.qualify_object_name(model, view.to)}"
        storage_sql = f" ENGINE = {view.engine}"
        if view.partition_by is not None:
            storage_sql += f" PARTITION BY {self.get_key_sql(model, view.partition_by)}"
        sort_keys_sql = ", ".join(self.get_key_sql(model, key) for key in view.get_sort_keys())
        if not sort_keys_sql:
            return storage_sql + " ORDER BY tuple()"
        return storage_sql + f" ORDER BY ({sort_keys_sql})" + CLICKHOUSE_MATERIALIZED_VIEW_STORAGE_SETTINGS_SQL

    def get_key_sql(self, model: type[Model], key: Any) -> str:
        """A key of a view's own storage in the SQL text.

        Args:
            model: The model declaring the view.
            key: A column of the view, an expression of hare over the model's fields the view selects
                under their own names, or a ``RawSQLTerm``.

        Returns:
            The quoted column; the expression in the connection's SQL; raw SQL as written.
        """
        if isinstance(key, str):
            return self.editor.quote(key)
        return DeclaredExpressions.get_sql(key, model, self.editor.client.dialect)

    async def create_materialized_view(self, model: type[Model], view: MaterializedView) -> None:
        """Creates a view and fills it with the rows its query gives, where it is declared with them.

        Raises:
            UnSupportedError: The view is filled on a cluster, its query reading a table that isn't distributed.
        """
        clickhouse_view = ClickhouseMaterializedView.from_materialized_view(view)
        fills_table = view.with_data and clickhouse_view.to is not None and clickhouse_view.refresh is None
        if fills_table:
            await self.raise_if_filled_on_one_server(model, view)
        await self.create_materialized_view_again(model, view)
        if fills_table:
            await self.fill_materialized_view(model, view)

    async def create_materialized_view_again(self, model: type[Model], view: MaterializedView) -> None:
        """Creates a view in place of the one it had - a storage of its own is filled, a table it
        writes to keeps its rows.

        Args:
            model: The model declaring the view.
            view: The view.

        Raises:
            UnSupportedError: The view's own storage is filled on a cluster, its query reading a table that
                isn't distributed.
        """
        clickhouse_view = ClickhouseMaterializedView.from_materialized_view(view)
        if view.with_data and clickhouse_view.refresh is None and clickhouse_view.to is None:
            await self.raise_if_filled_on_one_server(model, view)
        await self.editor.run_sqls(self.get_materialized_view_create_sqls(model, view))
        if not view.with_data:
            return
        if clickhouse_view.refresh is not None:
            await self.wait_for_refresh(model, view)
        elif clickhouse_view.to is None:
            await self.fill_materialized_view(model, view)

    async def raise_if_filled_on_one_server(self, model: type[Model], view: MaterializedView) -> None:
        """Refuses to fill a view on a cluster from tables keeping rows of each server's own - the fill runs on
        the server the connection is to, over its rows alone, and the rows of the others were left out.

        Args:
            model: The model declaring the view.
            view: The view.

        Raises:
            UnSupportedError: The connection is to a cluster, and the view's query reads a table that isn't
                distributed, or tables hare doesn't know (raw SQL without ``read_columns``).
        """
        cluster = getattr(self.editor.client, "cluster", None)
        if cluster is None or self.editor.collect_sql:
            return
        distributed_tables = await cluster.fetch_distributed_tables()
        read_tables = dict.fromkeys(table for table, _column in view.read_columns)
        local_tables = [
            table
            for table in read_tables
            if (cluster.database, table) not in distributed_tables
            and tuple(table.split(".", 1)) not in distributed_tables
        ]
        if local_tables or not read_tables:
            raise UnSupportedError(
                CLICKHOUSE_MATERIALIZED_VIEW_CLUSTER_FILL_MESSAGE.format(
                    model=model.__name__,
                    view=view.name,
                    read_tables=", ".join(local_tables) if local_tables else "tables it doesn't name in read_columns",
                )
            )

    async def fill_materialized_view(self, model: type[Model], view: MaterializedView) -> None:
        """Writes the rows a view's query gives now into the view.

        Args:
            model: The model declaring the view.
            view: The view.
        """
        await self.editor.run_sql(
            CLICKHOUSE_MATERIALIZED_VIEW_FILL_TEMPLATE.format(
                view=self.editor.qualify_object_name(model, view.name), query=self.editor.get_view_query_sql(view)
            )
        )

    async def wait_for_refresh(self, model: type[Model], view: MaterializedView) -> None:
        """Waits for the running refresh of a view refreshed on a schedule.

        Args:
            model: The model declaring the view.
            view: The view.
        """
        await self.editor.run_sql(
            CLICKHOUSE_VIEW_WAIT_TEMPLATE.format(view=self.editor.qualify_object_name(model, view.name))
        )

    async def drop_materialized_view(self, model: type[Model], view: MaterializedView) -> None:
        await self.editor.run_sql(
            CLICKHOUSE_VIEW_DROP_TEMPLATE.format(if_exists="", view=self.editor.qualify_object_name(model, view.name))
        )

    async def drop_model_materialized_views(self, model: type[Model]) -> None:
        for view in reversed(model._meta.materialized_views):
            await self.editor.run_sql(
                CLICKHOUSE_VIEW_DROP_TEMPLATE.format(
                    if_exists="IF EXISTS ", view=self.editor.qualify_object_name(model, view.name)
                )
            )

    async def alter_materialized_view(
        self, model: type[Model], old_view: MaterializedView, new_view: MaterializedView
    ) -> None:
        """Changes a view in place where the server does - the query of a view writing to a table,
        the schedule of a refreshed one - and creates it anew otherwise: a storage of its own is
        filled by the new query, a table written to keeps its rows."""
        old = ClickhouseMaterializedView.from_materialized_view(old_view)
        new = ClickhouseMaterializedView.from_materialized_view(new_view)
        view_sql = self.editor.qualify_object_name(model, new.name)
        if new.to is not None and new.refresh is None and dataclasses.replace(old, query=new.query) == new:
            await self.editor.run_sql(
                CLICKHOUSE_MATERIALIZED_VIEW_QUERY_TEMPLATE.format(
                    view=view_sql, query=self.editor.get_view_query_sql(new)
                )
            )
            return
        if (
            old.refresh is not None
            and new.refresh is not None
            and dataclasses.replace(old, refresh=new.refresh) == new
        ):
            await self.editor.run_sql(
                CLICKHOUSE_MATERIALIZED_VIEW_SCHEDULE_TEMPLATE.format(view=view_sql, schedule=new.refresh.strip())
            )
            return
        await self.drop_materialized_view(model, old_view)
        await self.create_materialized_view_again(model, new_view)

    async def rename_materialized_view(
        self, model: type[Model], old_view: MaterializedView, new_view: MaterializedView
    ) -> None:
        await ClickhouseViews(self.editor).rename_named_view(model, old_view.name, new_view.name)

    async def refresh_materialized_view(self, model: type[Model], view: MaterializedView, concurrently: bool) -> None:
        """Fills the view with the rows of its query again: a view refreshed on a schedule is
        refreshed now - its rows replaced at once; any other one is emptied and filled, its rows
        missing in between.

        Raises:
            UnSupportedError: ``concurrently`` for a view without a refresh schedule.
        """
        clickhouse_view = ClickhouseMaterializedView.from_materialized_view(view)
        view_sql = self.editor.qualify_object_name(model, view.name)
        if clickhouse_view.refresh is not None:
            await self.editor.run_sql(CLICKHOUSE_VIEW_REFRESH_TEMPLATE.format(view=view_sql))
            await self.wait_for_refresh(model, view)
            return
        if concurrently:
            raise UnSupportedError(
                f"Materialized view {view.name!r} can't be refreshed concurrently on ClickHouse - it is emptied and "
                "filled again; a ClickhouseMaterializedView(refresh=...) replaces its rows at once"
            )
        held_table_sql = view_sql
        if clickhouse_view.to is not None:
            held_table_sql = self.editor.qualify_object_name(model, clickhouse_view.to)
        await self.editor.run_sql(CLICKHOUSE_TABLE_TRUNCATE_TEMPLATE.format(table=held_table_sql))
        await self.fill_materialized_view(model, view)
