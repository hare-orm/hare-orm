from __future__ import annotations

from typing import Any, ClassVar

from hare.migrations.operations.schema_objects.object_types.materialized_view_object_type import (
    MaterializedViewObjectType,
)
from hare.migrations.operations.schema_objects.remove_schema_object import RemoveSchemaObject


class RemoveMaterializedView(RemoveSchemaObject):
    """Drops a materialized view of a model; going back creates it again from the state.

    Args:
        model_name: The model.
        name: The view's name.
        replacing: The migration adds a view of the name in its place - going back, a table the view
            writes into keeps its rows, not filled again.
    """

    object_type: ClassVar[type[MaterializedViewObjectType]] = MaterializedViewObjectType

    def __init__(self, model_name: str, name: str, *, replacing: bool = False) -> None:
        super().__init__(model_name, name)
        self.replacing = replacing

    def get_type_options(self) -> dict[str, Any]:
        return {"replacing": self.replacing}
