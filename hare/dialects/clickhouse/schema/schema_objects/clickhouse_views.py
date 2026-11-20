from __future__ import annotations

from typing import TYPE_CHECKING

from hare.ddl.schema_objects.view import View
from hare.dialects.base.schema.schema_objects.views import Views
from hare.dialects.clickhouse.schema.constants import (
    CLICKHOUSE_VIEW_CREATE_TEMPLATE,
    CLICKHOUSE_VIEW_DROP_TEMPLATE,
    CLICKHOUSE_VIEW_RENAME_TEMPLATE,
)

if TYPE_CHECKING:
    from hare.dialects.clickhouse.schema.clickhouse_schema_editor import ClickhouseSchemaEditor
    from hare.models.model_binding import ModelBinding


class ClickhouseViews(Views):
    """Views as ClickHouse writes it - a view is a table of the ``View`` engine: renamed as one, and
    not dropped with the table it reads."""

    editor: ClickhouseSchemaEditor

    __slots__ = ()

    def get_view_create_sqls(self, binding: ModelBinding, view: View, safe: bool = False) -> list[str]:
        return [
            CLICKHOUSE_VIEW_CREATE_TEMPLATE.format(
                on_cluster=self.editor.get_schema_cluster_sql(binding.schema),
                or_replace="OR REPLACE " if safe else "",
                view=self.editor.qualify_object_name(binding, view.name),
                query=self.editor.get_view_query_sql(view),
            )
        ]

    def get_view_drop_sql(self, binding: ModelBinding, view: View, *, if_exists: bool = False) -> str:
        return self.get_named_view_drop_sql(binding, view.name, if_exists=if_exists)

    def get_named_view_drop_sql(self, binding: ModelBinding, name: str, *, if_exists: bool = False) -> str:
        """Compile removal of either type of ClickHouse view."""
        return CLICKHOUSE_VIEW_DROP_TEMPLATE.format(
            on_cluster=self.editor.get_schema_cluster_sql(binding.schema),
            if_exists="IF EXISTS " if if_exists else "",
            view=self.editor.qualify_object_name(binding, name),
        )

    async def rename_view(self, binding: ModelBinding, old_view: View, new_view: View) -> None:
        await self.rename_named_view(binding, old_view.name, new_view.name)

    async def rename_named_view(self, binding: ModelBinding, old_name: str, new_name: str) -> None:
        """Renames a view of a model, a materialized one too - ClickHouse renames both as a table.

        Args:
            binding: The model.
            old_name: The view's name.
            new_name: Its new name.
        """
        if old_name == new_name:
            return
        await self.editor.run_sql(
            CLICKHOUSE_VIEW_RENAME_TEMPLATE.format(
                on_cluster=self.editor.get_schema_cluster_sql(binding.schema),
                view=self.editor.qualify_object_name(binding, old_name),
                new_view=self.editor.qualify_object_name(binding, new_name),
            )
        )
