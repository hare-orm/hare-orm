from __future__ import annotations

from hare.ddl.enums import GrantTarget
from hare.ddl.schema_objects.materialized_view import MaterializedView
from hare.dialects.base.schema.plans.schema_change_plan import SchemaChangePlan
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

    async def create_materialized_view_again(self, model: type[Model], view: MaterializedView) -> None:
        """Creates a materialized view in place of the one of its name a migration dropped - created as a
        new one where a view keeps its rows in a storage of its own.

        Args:
            model: The model declaring it.
            view: The view.
        """
        await self.create_materialized_view(model, view)

    async def drop_materialized_view(self, model: type[Model], view: MaterializedView) -> None:
        """Drops a materialized view of a model.

        Raises:
            UnSupportedError: The dialect has no materialized views.
        """
        raise self.get_unsupported_error("Materialized views")

    def get_materialized_view_drop_sql(
        self, model: type[Model], view: MaterializedView, *, if_exists: bool = False
    ) -> str:
        """Compile one removal for direct calls and dependency-ordered schema plans."""
        raise self.get_unsupported_error("Materialized views")

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
        statements = self.get_materialized_view_create_sqls(model, new_view)
        statements += self.editor.grants.get_grant_again_sqls(model, GrantTarget.MATERIALIZED_VIEW, new_view.name)
        await self.drop_materialized_view(model, old_view)
        await self.editor.run_sqls(statements)

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
            concurrently: Request a refresh that keeps it readable, subject to the dialect's
                requirements and supported refresh mechanism.

        Raises:
            UnSupportedError: The dialect has no materialized views.
        """
        raise self.get_unsupported_error("Materialized views")

    def get_schema_move_plan(
        self, model: type[Model], materialized_view: MaterializedView, old_schema: str | None, new_schema: str | None
    ) -> SchemaChangePlan:
        """Prepares moving a declared object with its model to another schema.

        Args:
            model: The model, rendered in its old schema.
            materialized_view: The object.
            old_schema: The schema it is in; None for the connection's current schema.
            new_schema: The schema it moves to; None for the connection's current schema.

        Raises:
            UnSupportedError: The dialect moves none.
        """
        raise self.get_unsupported_error("Materialized views moved to another schema")
