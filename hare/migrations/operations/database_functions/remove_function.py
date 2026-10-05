from __future__ import annotations

from typing import ClassVar

from hare.migrations.operations.schema_objects.object_types.function_object_type import FunctionObjectType
from hare.migrations.operations.schema_objects.remove_schema_object import RemoveSchemaObject


class RemoveFunction(RemoveSchemaObject):
    """Drops a function of a model; going back creates it again from the state.

    Args:
        model_name: The model.
        name: The function's name.
    """

    object_type: ClassVar[type[FunctionObjectType]] = FunctionObjectType

    def __init__(self, model_name: str, name: str) -> None:
        super().__init__(model_name, name)
