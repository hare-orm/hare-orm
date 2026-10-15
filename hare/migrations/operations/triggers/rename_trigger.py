from __future__ import annotations

from typing import ClassVar

from hare.migrations.operations.schema_objects.object_types.trigger_object_type import TriggerObjectType
from hare.migrations.operations.schema_objects.rename_schema_object import RenameSchemaObject


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
