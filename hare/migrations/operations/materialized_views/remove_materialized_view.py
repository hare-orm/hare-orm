from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

from hare.migrations.operations.schema_objects.object_types.materialized_view_object_type import (
    MaterializedViewObjectType,
)
from hare.migrations.operations.schema_objects.remove_schema_object import RemoveSchemaObject
from hare.migrations.reports.operation_effect import OperationEffect

if TYPE_CHECKING:
    from hare.dialects.base.schema.base_schema_editor import BaseSchemaEditor
    from hare.migrations.state.state import State


class RemoveMaterializedView(RemoveSchemaObject):
    """Drops a materialized view of a model; going back creates it again from the state - a storage of its
    own filled by its query, a table it writes into keeping the rows the drop left in it, not filled again.

    Args:
        model_name: The model.
        name: The view's name.
    """

    object_type: ClassVar[type[MaterializedViewObjectType]] = MaterializedViewObjectType

    def get_effect(
        self, app_label: str, old_state: State, new_state: State, schema_editor: BaseSchemaEditor
    ) -> OperationEffect:
        view = self.find(old_state, app_label)
        return OperationEffect(
            operation=self,
            reversible=self.reversible,
            loses_data=view.has_internal_storage,
            reason=f"drops the stored rows of materialized view {view.name!r}" if view.has_internal_storage else None,
        )

    def get_type_options(self) -> dict[str, Any]:
        # The drop left a table the view writes into with its rows.
        return {"replacing": True}
