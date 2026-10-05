from __future__ import annotations

from typing import TYPE_CHECKING

from hare.exceptions import ConfigurationError
from hare.migrations.operations.model_bound_operation import ModelBoundOperation
from hare.migrations.operations.schema_objects.object_types.materialized_view_object_type import (
    MaterializedViewObjectType,
)

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.schema.base_schema_editor import BaseSchemaEditor
    from hare.migrations.state.state import State


class RefreshMaterializedView(ModelBoundOperation):
    """Fills a materialized view of a model with the rows of its query again - after a data
    migration, say. Changes no state; going back does nothing, the rows refreshed stay.

    Args:
        model_name: The model.
        name: The view's name.
        concurrently: Keep the view readable while it refreshes - needs its ``unique_columns``.

    Raises:
        ConfigurationError: ``concurrently`` isn't a bool.
    """

    reads_old_state = False

    def __init__(self, model_name: str, name: str, concurrently: bool = False) -> None:
        if not isinstance(concurrently, bool):
            raise ConfigurationError(f"RefreshMaterializedView.concurrently must be a bool, got {concurrently!r}")
        self.model_name = model_name
        self.name = name
        self.concurrently = concurrently

    def describe(self) -> str:
        action = "Concurrently refresh" if self.concurrently else "Refresh"
        return f"{action} materialized view {self.name} of {self.model_name}"

    def reaches_other_models(self) -> bool:
        """The view's query may read any table."""
        return True

    def state_forward(self, app_label: str, state: State) -> None:
        return None

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
        model_state = self.get_model_state(new_state, app_label, self.model_name)
        view = MaterializedViewObjectType.find(model_state, lambda: self._model(new_state, app_label), name=self.name)
        await state_editor.materialized_views.refresh_materialized_view(
            self._model(new_state, app_label), view, self.concurrently
        )
