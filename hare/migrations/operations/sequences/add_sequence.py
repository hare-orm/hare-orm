from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

from hare.migrations.operations.schema_objects.add_schema_object import AddSchemaObject
from hare.migrations.operations.schema_objects.object_types.sequence_object_type import SequenceObjectType

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.ddl.schema_objects.database_sequence import DatabaseSequence


class AddSequence(AddSchemaObject):
    """Creates a sequence a model declares, owned by its column; going back drops it.

    Args:
        model_name: The model.
        sequence: The sequence.
    """

    object_type: ClassVar[type[SequenceObjectType]] = SequenceObjectType

    def __init__(self, model_name: str, sequence: DatabaseSequence) -> None:
        super().__init__(model_name)
        self.sequence = sequence

    @property
    def schema_object(self) -> Any:
        return self.sequence
