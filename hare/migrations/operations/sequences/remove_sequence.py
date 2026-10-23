from __future__ import annotations

from typing import ClassVar

from hare.migrations.operations.schema_objects.object_types.sequence_object_type import SequenceObjectType
from hare.migrations.operations.schema_objects.remove_schema_object import RemoveSchemaObject


class RemoveSequence(RemoveSchemaObject):
    """Drops a sequence of a model; going back creates it again from the state - starting over.

    Args:
        model_name: The model.
        name: The sequence's name.
    """

    object_type: ClassVar[type[SequenceObjectType]] = SequenceObjectType

    def __init__(self, model_name: str, name: str) -> None:
        super().__init__(model_name, name)
