from __future__ import annotations

from typing import ClassVar

from hare.migrations.operations.schema_objects.object_types.trigger_object_type import TriggerObjectType
from hare.migrations.operations.schema_objects.remove_schema_object import RemoveSchemaObject


class RemoveTrigger(RemoveSchemaObject):
    """Removes a trigger from a model.

    Args:
        model_name: The model.
        name: The trigger's name.
    """

    object_type: ClassVar[type[TriggerObjectType]] = TriggerObjectType

    def __init__(self, model_name: str, name: str) -> None:
        super().__init__(model_name, name)
