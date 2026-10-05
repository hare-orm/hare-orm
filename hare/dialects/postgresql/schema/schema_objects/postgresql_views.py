from __future__ import annotations

from typing import TYPE_CHECKING

from hare.ddl.schema_objects.view import View
from hare.dialects.base.schema.schema_objects.views import Views
from hare.dialects.postgresql.schema.constants import (
    POSTGRESQL_VIEW_CREATE_TEMPLATE,
    POSTGRESQL_VIEW_DROP_TEMPLATE,
    POSTGRESQL_VIEW_RENAME_TEMPLATE,
)
from hare.models import Model

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.postgresql.schema.postgresql_schema_editor import PostgresqlSchemaEditor


class PostgresqlViews(Views):
    """Views as PostgreSQL writes it."""

    __slots__ = ()

    editor: PostgresqlSchemaEditor

    def get_view_create_sqls(self, model: type[Model], view: View, safe: bool = False) -> list[str]:
        return [
            POSTGRESQL_VIEW_CREATE_TEMPLATE.format(
                or_replace="OR REPLACE " if safe else "",
                view=self.editor.qualify_object_name(model, view.name),
                query=self.editor.get_view_query_sql(view),
            )
        ]

    async def drop_view(self, model: type[Model], view: View) -> None:
        await self.editor.run_sql(
            POSTGRESQL_VIEW_DROP_TEMPLATE.format(view=self.editor.qualify_object_name(model, view.name))
        )

    async def rename_view(self, model: type[Model], old_view: View, new_view: View) -> None:
        if old_view.name == new_view.name:
            return
        await self.editor.run_sql(
            POSTGRESQL_VIEW_RENAME_TEMPLATE.format(
                view=self.editor.qualify_object_name(model, old_view.name), new_name=self.editor.quote(new_view.name)
            )
        )
