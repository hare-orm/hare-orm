from __future__ import annotations

from typing import TYPE_CHECKING

from hare.migrations.state.project.state import State

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.schema.editor import BaseSchemaEditor


class ModelTableOperation:
    """Creates or drops the table of the model an operation names - ``CreateModel`` creates it
    forwards and drops it backwards, ``DeleteModel`` the other way round. A state-only operation
    and an unmanaged model (``Meta.managed = False``) leave the database alone."""

    name: str
    state_only: bool

    async def _create_model_table(self, app_label: str, state: State, state_editor: BaseSchemaEditor | None) -> None:
        """Creates the table of the model as ``state`` has it.

        Args:
            app_label: The operation's app.
            state: The state holding the model.
            state_editor: The schema editor, None when there is no database.
        """
        if not state_editor or self.state_only:
            return
        model = state.apps.get_model(f"{app_label}.{self.name}")
        if model._meta.managed is not False:
            await state_editor.create_model(model)

    async def _delete_model_table(self, app_label: str, state: State, state_editor: BaseSchemaEditor | None) -> None:
        """Drops the table of the model as ``state`` has it.

        Args:
            app_label: The operation's app.
            state: The state holding the model.
            state_editor: The schema editor, None when there is no database.
        """
        if not state_editor or self.state_only:
            return
        model = state.apps.get_model(f"{app_label}.{self.name}")
        if model._meta.managed is not False:
            await state_editor.delete_model(model)
