from __future__ import annotations

from hare.ddl.enums import GrantTarget
from hare.ddl.schema_objects.view import View
from hare.dialects.base.schema.schema_editor_part import SchemaEditorPart
from hare.models import Model


class Views(SchemaEditorPart):
    """Views a model declares: created, dropped, altered and renamed."""

    __slots__ = ()

    def get_view_create_sqls(self, model: type[Model], view: View, safe: bool = False) -> list[str]:
        """The statements creating a view of a model.

        Args:
            model: The model declaring it.
            view: The view.
            safe: Replace a view of the same name.

        Raises:
            UnSupportedError: The dialect has no views.
        """
        raise self.get_unsupported_error("Views")

    async def create_view(self, model: type[Model], view: View) -> None:
        """Creates a view of a model."""
        await self.editor.run_sqls(self.get_view_create_sqls(model, view))

    async def drop_view(self, model: type[Model], view: View) -> None:
        """Drops a view of a model.

        Raises:
            UnSupportedError: The dialect has no views.
        """
        raise self.get_unsupported_error("Views")

    async def drop_model_views(self, model: type[Model]) -> None:
        """Drops the views a model declares, before its table is dropped - nothing by default: a
        dialect without views has none, and one dropping them otherwise does it its own way.

        Args:
            model: The model.
        """

    async def alter_view(self, model: type[Model], old_view: View, new_view: View) -> None:
        """Replaces a view of a model by its new version of the same name, with the grants on it.

        Args:
            model: The model, rendered with its grants after the change.
            old_view: The view as it is.
            new_view: The view as it becomes.
        """
        await self.drop_view(model, old_view)
        await self.create_view(model, new_view)
        await self.editor.grants.grant_again(model, GrantTarget.VIEW, new_view.name)

    async def alter_changed_views(self, old_model: type[Model], new_model: type[Model]) -> None:
        """Recreates every view of a model whose definition a rename changed - a database following a
        renamed column in a view keeps the view's column under the old name, one storing the view's
        text keeps reading the old column or table.

        Args:
            old_model: The model before the rename.
            new_model: The model after it.
        """
        old_views_by_name = {view.name: view for view in old_model._meta.views}
        for new_view in new_model._meta.views:
            old_view = old_views_by_name.get(new_view.name)
            if old_view is not None and old_view != new_view:
                await self.alter_view(new_model, old_view, new_view)

    async def rename_view(self, model: type[Model], old_view: View, new_view: View) -> None:
        """Renames a view of a model.

        Raises:
            UnSupportedError: The dialect has no views.
        """
        raise self.get_unsupported_error("Views")
