from __future__ import annotations

from typing import TYPE_CHECKING

from hare.ddl.schema_objects.materialized_view import MaterializedView
from hare.dialects.base.schema.schema_objects.materialized_views import MaterializedViews
from hare.dialects.postgresql.schema.constants import (
    POSTGRESQL_INDEX_RENAME_TEMPLATE,
    POSTGRESQL_MATERIALIZED_VIEW_CREATE_TEMPLATE,
    POSTGRESQL_MATERIALIZED_VIEW_DROP_TEMPLATE,
    POSTGRESQL_MATERIALIZED_VIEW_REFRESH_TEMPLATE,
    POSTGRESQL_MATERIALIZED_VIEW_RENAME_TEMPLATE,
    POSTGRESQL_MATERIALIZED_VIEW_UNIQUE_INDEX_SUFFIX,
    POSTGRESQL_MATERIALIZED_VIEW_UNIQUE_INDEX_TEMPLATE,
)
from hare.exceptions import ConfigurationError
from hare.models import Model
from hare.sql.identifiers import Identifiers

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.postgresql.schema.postgresql_schema_editor import PostgresqlSchemaEditor


class PostgresqlMaterializedViews(MaterializedViews):
    """MaterializedViews as PostgreSQL writes it."""

    __slots__ = ()

    editor: PostgresqlSchemaEditor

    def get_materialized_view_create_sqls(
        self, model: type[Model], view: MaterializedView, safe: bool = False
    ) -> list[str]:
        qualified_view = self.editor.qualify_object_name(model, view.name)
        if_not_exists = "IF NOT EXISTS " if safe else ""
        statements = [
            POSTGRESQL_MATERIALIZED_VIEW_CREATE_TEMPLATE.format(
                if_not_exists=if_not_exists,
                view=qualified_view,
                query=self.editor.get_view_query_sql(view),
                data="DATA" if view.with_data else "NO DATA",
            )
        ]
        if view.unique_columns:
            statements.append(
                POSTGRESQL_MATERIALIZED_VIEW_UNIQUE_INDEX_TEMPLATE.format(
                    if_not_exists=if_not_exists,
                    index_name=self.editor.quote(self.get_materialized_view_index_name(view)),
                    view=qualified_view,
                    columns=", ".join(self.editor.quote(column) for column in view.unique_columns),
                )
            )
        return statements

    async def drop_materialized_view(self, model: type[Model], view: MaterializedView) -> None:
        await self.editor.run_sql(
            POSTGRESQL_MATERIALIZED_VIEW_DROP_TEMPLATE.format(view=self.editor.qualify_object_name(model, view.name))
        )

    async def rename_materialized_view(
        self, model: type[Model], old_view: MaterializedView, new_view: MaterializedView
    ) -> None:
        if old_view.name == new_view.name:
            return
        await self.editor.run_sql(
            POSTGRESQL_MATERIALIZED_VIEW_RENAME_TEMPLATE.format(
                view=self.editor.qualify_object_name(model, old_view.name), new_name=self.editor.quote(new_view.name)
            )
        )
        if old_view.unique_columns:
            await self.editor.run_sql(
                POSTGRESQL_INDEX_RENAME_TEMPLATE.format(
                    index_name=self.editor.qualify_object_name(model, self.get_materialized_view_index_name(old_view)),
                    new_name=self.editor.quote(self.get_materialized_view_index_name(new_view)),
                )
            )

    async def refresh_materialized_view(self, model: type[Model], view: MaterializedView, concurrently: bool) -> None:
        """Refreshes the view; concurrently it stays readable, which needs the unique index of its
        ``unique_columns`` and a view filled before.

        Raises:
            ConfigurationError: ``concurrently`` for a view without ``unique_columns``.
        """
        if concurrently and not view.unique_columns:
            raise ConfigurationError(
                f"Materialized view {view.name!r} can't be refreshed concurrently without unique_columns - "
                "a concurrent refresh needs a unique index on it"
            )
        await self.editor.run_sql(
            POSTGRESQL_MATERIALIZED_VIEW_REFRESH_TEMPLATE.format(
                concurrently="CONCURRENTLY " if concurrently else "",
                view=self.editor.qualify_object_name(model, view.name),
            )
        )

    @staticmethod
    def get_materialized_view_index_name(view: MaterializedView) -> str:
        """The name of the unique index of a materialized view's ``unique_columns``."""
        return Identifiers.get_within_limit(f"{view.name}{POSTGRESQL_MATERIALIZED_VIEW_UNIQUE_INDEX_SUFFIX}")
