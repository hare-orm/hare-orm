from __future__ import annotations

from typing import ClassVar

from hare.migrations.operations.schema_objects.object_types.materialized_view_object_type import (
    MaterializedViewObjectType,
)
from hare.migrations.operations.schema_objects.remove_schema_object import RemoveSchemaObject


class RemoveMaterializedView(RemoveSchemaObject):
    """Drops a materialized view of a model; going back creates it again from the state.

    Args:
        model_name: The model.
        name: The view's name.
    """

    object_type: ClassVar[type[MaterializedViewObjectType]] = MaterializedViewObjectType

    def __init__(self, model_name: str, name: str) -> None:
        super().__init__(model_name, name)
