from __future__ import annotations

from typing import ClassVar

from hare.migrations.operations.schema_objects.object_types.view_object_type import ViewObjectType
from hare.migrations.operations.schema_objects.remove_schema_object import RemoveSchemaObject


class RemoveView(RemoveSchemaObject):
    """Drops a view of a model; going back creates it again from the state.

    Args:
        model_name: The model.
        name: The view's name.
    """

    object_type: ClassVar[type[ViewObjectType]] = ViewObjectType

    def __init__(self, model_name: str, name: str) -> None:
        super().__init__(model_name, name)
