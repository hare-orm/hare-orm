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

    async def rename_view(self, model: type[Model], old_view: View, new_view: View) -> None:
        """Renames a view of a model.

        Raises:
            UnSupportedError: The dialect has no views.
        """
        raise self.get_unsupported_error("Views")
