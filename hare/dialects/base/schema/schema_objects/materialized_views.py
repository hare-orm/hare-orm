from __future__ import annotations

from hare.ddl.enums import GrantTarget
from hare.ddl.schema_objects.materialized_view import MaterializedView
from hare.dialects.base.schema.schema_editor_part import SchemaEditorPart
from hare.models import Model


class MaterializedViews(SchemaEditorPart):
    """Materialized views a model declares: created, dropped, altered, renamed and refreshed."""

    __slots__ = ()

    def get_materialized_view_create_sqls(
        self, model: type[Model], view: MaterializedView, safe: bool = False
    ) -> list[str]:
        """The statements creating a materialized view of a model, with its unique index.

        Args:
            model: The model declaring it.
            view: The view.
            safe: Create it only when it doesn't exist yet.

        Raises:
            UnSupportedError: The dialect has no materialized views.
        """
        raise self.get_unsupported_error("Materialized views")

    async def create_materialized_view(self, model: type[Model], view: MaterializedView) -> None:
        """Creates a materialized view of a model."""
        await self.editor.run_sqls(self.get_materialized_view_create_sqls(model, view))

    async def drop_materialized_view(self, model: type[Model], view: MaterializedView) -> None:
        """Drops a materialized view of a model.

        Raises:
            UnSupportedError: The dialect has no materialized views.
        """
        raise self.get_unsupported_error("Materialized views")

    async def drop_model_materialized_views(self, model: type[Model]) -> None:
        """Drops the materialized views a model declares, before its table is dropped - nothing by
        default: a dialect without them has none, and one dropping them otherwise does it its own way.

        Args:
            model: The model.
        """

    async def alter_materialized_view(
        self, model: type[Model], old_view: MaterializedView, new_view: MaterializedView
    ) -> None:
        """Replaces a materialized view of a model by its new version of the same name, with the
        grants on it.

        Args:
            model: The model, rendered with its grants after the change.
            old_view: The view as it is.
            new_view: The view as it becomes.
        """
        await self.drop_materialized_view(model, old_view)
        await self.create_materialized_view(model, new_view)
        await self.editor.grants.grant_again(model, GrantTarget.MATERIALIZED_VIEW, new_view.name)

    async def rename_materialized_view(
        self, model: type[Model], old_view: MaterializedView, new_view: MaterializedView
    ) -> None:
        """Renames a materialized view of a model, with its unique index.

        Raises:
            UnSupportedError: The dialect has no materialized views.
        """
        raise self.get_unsupported_error("Materialized views")

    async def refresh_materialized_view(self, model: type[Model], view: MaterializedView, concurrently: bool) -> None:
        """Fills a materialized view of a model with the rows of its query again.

        Args:
            model: The model declaring it.
            view: The view.
            concurrently: Keep it readable while it refreshes - needs its ``unique_columns``.

        Raises:
            UnSupportedError: The dialect has no materialized views.
        """
        raise self.get_unsupported_error("Materialized views")
