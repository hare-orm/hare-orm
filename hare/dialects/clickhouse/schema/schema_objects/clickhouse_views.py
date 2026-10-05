from __future__ import annotations

from hare.ddl.schema_objects.view import View
from hare.dialects.base.schema.schema_objects.views import Views
from hare.dialects.clickhouse.schema.constants import (
    CLICKHOUSE_VIEW_CREATE_TEMPLATE,
    CLICKHOUSE_VIEW_DROP_TEMPLATE,
    CLICKHOUSE_VIEW_RENAME_TEMPLATE,
)
from hare.models import Model


class ClickhouseViews(Views):
    """Views as ClickHouse writes it - a view is a table of the ``View`` engine: renamed as one, and
    not dropped with the table it reads."""

    __slots__ = ()

    def get_view_create_sqls(self, model: type[Model], view: View, safe: bool = False) -> list[str]:
        return [
            CLICKHOUSE_VIEW_CREATE_TEMPLATE.format(
                or_replace="OR REPLACE " if safe else "",
                view=self.editor.qualify_object_name(model, view.name),
                query=self.editor.get_view_query_sql(view),
            )
        ]

    async def drop_view(self, model: type[Model], view: View) -> None:
        await self.editor.run_sql(
            CLICKHOUSE_VIEW_DROP_TEMPLATE.format(if_exists="", view=self.editor.qualify_object_name(model, view.name))
        )

    async def drop_model_views(self, model: type[Model]) -> None:
        for view in reversed(model._meta.views):
            await self.editor.run_sql(
                CLICKHOUSE_VIEW_DROP_TEMPLATE.format(
                    if_exists="IF EXISTS ", view=self.editor.qualify_object_name(model, view.name)
                )
            )

    async def rename_view(self, model: type[Model], old_view: View, new_view: View) -> None:
        if old_view.name == new_view.name:
            return
        await self.editor.run_sql(
            CLICKHOUSE_VIEW_RENAME_TEMPLATE.format(
                view=self.editor.qualify_object_name(model, old_view.name),
                new_view=self.editor.qualify_object_name(model, new_view.name),
            )
        )
