from __future__ import annotations

from hare.ddl.enums import GrantTarget
from hare.ddl.schema_objects.view import View
from hare.dialects.base.schema.plans.schema_change_plan import SchemaChangePlan
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

    def get_view_drop_sql(self, model: type[Model], view: View, *, if_exists: bool = False) -> str:
        """Compile one removal for direct calls and dependency-ordered schema plans."""
        raise self.get_unsupported_error("Views")

    async def alter_view(self, model: type[Model], old_view: View, new_view: View) -> None:
        """Replaces a view of a model by its new version of the same name, with the grants on it.

        Args:
            model: The model, rendered with its grants after the change.
            old_view: The view as it is.
            new_view: The view as it becomes.
        """
        statements = self.get_view_create_sqls(model, new_view)
        statements += self.editor.grants.get_grant_again_sqls(model, GrantTarget.VIEW, new_view.name)
        await self.drop_view(model, old_view)
        await self.editor.run_sqls(statements)

    async def rename_view(self, model: type[Model], old_view: View, new_view: View) -> None:
        """Renames a view of a model.

        Raises:
            UnSupportedError: The dialect has no views.
        """
        raise self.get_unsupported_error("Views")

    def get_schema_move_plan(
        self, model: type[Model], view: View, old_schema: str | None, new_schema: str | None
    ) -> SchemaChangePlan:
        """Prepares moving a declared object with its model to another schema.

        Args:
            model: The model, rendered in its old schema.
            view: The object.
            old_schema: The schema it is in; None for the connection's current schema.
            new_schema: The schema it moves to; None for the connection's current schema.

        Raises:
            UnSupportedError: The dialect moves none.
        """
        raise self.get_unsupported_error("Views moved to another schema")
