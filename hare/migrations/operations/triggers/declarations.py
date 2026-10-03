from __future__ import annotations

from typing import ClassVar

from hare.migrations.operations.schema_objects.remove_schema_object import RemoveSchemaObject
from hare.migrations.operations.schema_objects.rename_schema_object import RenameSchemaObject
from hare.migrations.operations.schema_objects.trigger_object_type import TriggerObjectType


class RemoveTrigger(RemoveSchemaObject):
    """Removes a trigger from a model.

    Args:
        model_name: The model.
        name: The trigger's name.
    """

    object_type: ClassVar[type[TriggerObjectType]] = TriggerObjectType

    def __init__(self, model_name: str, name: str) -> None:
        super().__init__(model_name, name)


class RenameTrigger(RenameSchemaObject):
    """Renames a trigger of a model.

    Args:
        model_name: The model.
        old_name: The old name.
        new_name: The new name.
    """

    object_type: ClassVar[type[TriggerObjectType]] = TriggerObjectType

    def __init__(self, model_name: str, old_name: str, new_name: str) -> None:
        super().__init__(model_name, new_name, old_name=old_name)
