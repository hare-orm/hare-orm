from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from hare.models.model_binding import ModelBinding

from hare.ddl.enums import GrantTarget
from hare.ddl.schema_objects.view import View
from hare.dialects.base.schema.plans.schema_change_plan import SchemaChangePlan
from hare.dialects.base.schema.schema_editor_part import SchemaEditorPart


class Views(SchemaEditorPart):
    """Views a model declares: created, dropped, altered and renamed."""

    __slots__ = ()

    def get_view_create_sqls(self, binding: ModelBinding, view: View, safe: bool = False) -> list[str]:
        """The statements creating a view of a model.

        Args:
            binding: The model declaring it.
            view: The view.
            safe: Replace a view of the same name.

        Raises:
            UnSupportedError: The dialect has no views.
        """
        raise self.get_unsupported_error("Views")

    async def create_view(self, binding: ModelBinding, view: View) -> None:
        """Creates a view of a model."""
        await self.editor.run_sqls(self.get_view_create_sqls(binding, view))

    async def drop_view(self, binding: ModelBinding, view: View) -> None:
        """Drops a view of a model.

        Raises:
            UnSupportedError: The dialect has no views.
        """
        await self.editor.run_sql(self.get_view_drop_sql(binding, view))

    def get_view_drop_sql(self, binding: ModelBinding, view: View, *, if_exists: bool = False) -> str:
        """Compile one removal for direct calls and dependency-ordered schema plans."""
        raise self.get_unsupported_error("Views")

    async def alter_view(self, binding: ModelBinding, old_view: View, new_view: View) -> None:
        """Replaces a view of a model by its new version of the same name, with the grants on it.

        Args:
            binding: The model, rendered with its grants after the change.
            old_view: The view as it is.
            new_view: The view as it becomes.
        """
        statements = self.get_view_create_sqls(binding, new_view)
        statements += self.editor.grants.get_grant_again_sqls(binding, GrantTarget.VIEW, new_view.name)
        await self.drop_view(binding, old_view)
        await self.editor.run_sqls(statements)

    async def rename_view(self, binding: ModelBinding, old_view: View, new_view: View) -> None:
        """Renames a view of a model.

        Raises:
            UnSupportedError: The dialect has no views.
        """
        raise self.get_unsupported_error("Views")

    def get_schema_move_plan(
        self, binding: ModelBinding, view: View, old_schema: str | None, new_schema: str | None
    ) -> SchemaChangePlan:
        """Prepares moving a declared object with its model to another schema.

        Args:
            binding: The model, rendered in its old schema.
            view: The object.
            old_schema: The schema it is in; None for the connection's current schema.
            new_schema: The schema it moves to; None for the connection's current schema.

        Raises:
            UnSupportedError: The dialect moves none.
        """
        raise self.get_unsupported_error("Views moved to another schema")
