from __future__ import annotations

from typing import TYPE_CHECKING

from hare.ddl.schema_objects.view import View
from hare.dialects.base.schema.schema_objects.views import Views
from hare.dialects.clickhouse.schema.constants import (
    CLICKHOUSE_VIEW_CREATE_TEMPLATE,
    CLICKHOUSE_VIEW_DROP_TEMPLATE,
    CLICKHOUSE_VIEW_RENAME_TEMPLATE,
)
from hare.models import Model

if TYPE_CHECKING:
    from hare.dialects.clickhouse.schema.clickhouse_schema_editor import ClickhouseSchemaEditor


class ClickhouseViews(Views):
    """Views as ClickHouse writes it - a view is a table of the ``View`` engine: renamed as one, and
    not dropped with the table it reads."""

    editor: ClickhouseSchemaEditor

    __slots__ = ()

    def get_view_create_sqls(self, model: type[Model], view: View, safe: bool = False) -> list[str]:
        return [
            CLICKHOUSE_VIEW_CREATE_TEMPLATE.format(
                on_cluster=self.editor.get_schema_cluster_sql(model._meta.schema),
                or_replace="OR REPLACE " if safe else "",
                view=self.editor.qualify_object_name(model, view.name),
                query=self.editor.get_view_query_sql(view),
            )
        ]

    async def drop_view(self, model: type[Model], view: View) -> None:
        await self.editor.run_sql(self.get_view_drop_sql(model, view))

    def get_view_drop_sql(self, model: type[Model], view: View, *, if_exists: bool = False) -> str:
        return CLICKHOUSE_VIEW_DROP_TEMPLATE.format(
            on_cluster=self.editor.get_schema_cluster_sql(model._meta.schema),
            if_exists="IF EXISTS " if if_exists else "",
            view=self.editor.qualify_object_name(model, view.name),
        )

    async def rename_view(self, model: type[Model], old_view: View, new_view: View) -> None:
        await self.rename_named_view(model, old_view.name, new_view.name)

    async def rename_named_view(self, model: type[Model], old_name: str, new_name: str) -> None:
        """Renames a view of a model, a materialized one too - ClickHouse renames both as a table.

        Args:
            model: The model.
            old_name: The view's name.
            new_name: Its new name.
        """
        if old_name == new_name:
            return
        await self.editor.run_sql(
            CLICKHOUSE_VIEW_RENAME_TEMPLATE.format(
                on_cluster=self.editor.get_schema_cluster_sql(model._meta.schema),
                view=self.editor.qualify_object_name(model, old_name),
                new_view=self.editor.qualify_object_name(model, new_name),
            )
        )
