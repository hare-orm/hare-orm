from __future__ import annotations

from typing import TYPE_CHECKING

from hare.migrations.operations.model_bound_operation import ModelBoundOperation
from hare.migrations.operations.schema_objects.object_types.materialized_view_object_type import (
    MaterializedViewObjectType,
)

if TYPE_CHECKING:
    from hare.ddl.schema_objects.materialized_view import MaterializedView
    from hare.dialects.base.schema.base_schema_editor import BaseSchemaEditor
    from hare.migrations.state.state import State
    from hare.models.model_binding import ModelBinding


class RefreshMaterializedView(ModelBoundOperation):
    """Fills a materialized view of a model with the rows of its query again - after a data
    migration, say. Changes no state; going back does nothing, the rows refreshed stay.

    Args:
        model_name: The model.
        name: The view's name.
        concurrently: Request a refresh that keeps the view readable. Requirements and support
            belong to the dialect; a ``unique_columns`` declaration may be required.
    """

    def __init__(self, model_name: str, name: str, concurrently: bool = False) -> None:
        self.model_name = model_name
        self.name = name
        self.concurrently = concurrently

    def describe(self) -> str:
        action = "Concurrently refresh" if self.concurrently else "Refresh"
        return f"{action} materialized view {self.name} of {self.model_name}"

    def reaches_other_models(self) -> bool:
        """The view's query may read any table."""
        return True

    def _get_refresh_target(self, state: State, app_label: str) -> tuple[ModelBinding, MaterializedView]:
        """The bound model and named view to check or refresh."""
        model_state = self.get_model_state(state, app_label, self.model_name)
        binding = self.get_model_binding(state, app_label)
        view = MaterializedViewObjectType.find(model_state, lambda: binding, name=self.name)
        return binding, view

    def raise_if_unsupported(
        self, app_label: str, old_state: State, new_state: State, schema_editor: BaseSchemaEditor
    ) -> None:
        MaterializedViewObjectType.raise_if_unsupported(schema_editor)
        binding, view = self._get_refresh_target(new_state, app_label)
        schema_editor.materialized_views.check_refresh(binding, view, self.concurrently)

    async def database_forward(
        self,
        app_label: str,
        old_state: State,
        new_state: State,
        state_editor: BaseSchemaEditor | None = None,
    ) -> None:
        if not state_editor:
            return
        MaterializedViewObjectType.raise_if_unsupported(state_editor)
        binding, view = self._get_refresh_target(new_state, app_label)
        await state_editor.materialized_views.refresh_materialized_view(binding, view, self.concurrently)
