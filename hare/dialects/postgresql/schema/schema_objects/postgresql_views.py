from __future__ import annotations

from typing import TYPE_CHECKING

from hare.ddl.schema_objects.view import View
from hare.dialects.base.schema.plans.schema_change_plan import SchemaChangePlan
from hare.dialects.base.schema.schema_objects.views import Views
from hare.dialects.postgresql.schema.constants import (
    POSTGRESQL_VIEW_CREATE_TEMPLATE,
    POSTGRESQL_VIEW_DROP_IF_EXISTS_TEMPLATE,
    POSTGRESQL_VIEW_DROP_TEMPLATE,
    POSTGRESQL_VIEW_RENAME_TEMPLATE,
)

if TYPE_CHECKING:
    from hare.dialects.postgresql.schema.postgresql_schema_editor import PostgresqlSchemaEditor
    from hare.models.model_binding import ModelBinding


class PostgresqlViews(Views):
    """Views as PostgreSQL writes it."""

    __slots__ = ()

    editor: PostgresqlSchemaEditor

    def get_view_create_sqls(self, binding: ModelBinding, view: View, safe: bool = False) -> list[str]:
        return [
            POSTGRESQL_VIEW_CREATE_TEMPLATE.format(
                or_replace="OR REPLACE " if safe else "",
                view=self.editor.qualify_object_name(binding, view.name),
                query=self.editor.get_view_query_sql(view),
            )
        ]

    def get_view_drop_sql(self, binding: ModelBinding, view: View, *, if_exists: bool = False) -> str:
        """Compile removal without changing the catalog."""
        template = POSTGRESQL_VIEW_DROP_IF_EXISTS_TEMPLATE if if_exists else POSTGRESQL_VIEW_DROP_TEMPLATE
        return template.format(view=self.editor.qualify_object_name(binding, view.name))

    async def rename_view(self, binding: ModelBinding, old_view: View, new_view: View) -> None:
        if old_view.name == new_view.name:
            return
        await self.editor.run_sql(
            POSTGRESQL_VIEW_RENAME_TEMPLATE.format(
                view=self.editor.qualify_object_name(binding, old_view.name), new_name=self.editor.quote(new_view.name)
            )
        )

    def get_schema_move_plan(
        self, binding: ModelBinding, view: View, old_schema: str | None, new_schema: str | None
    ) -> SchemaChangePlan:
        return self.editor.schemas.get_object_schema_move_plan(
            "VIEW", self.editor.qualify_table_name(view.name, old_schema), new_schema
        )
