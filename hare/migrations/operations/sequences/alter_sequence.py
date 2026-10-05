from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

from hare.migrations.operations.schema_objects.alter_schema_object import AlterSchemaObject
from hare.migrations.operations.schema_objects.object_types.sequence_object_type import SequenceObjectType

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.ddl.schema_objects.database_sequence import DatabaseSequence


class AlterSequence(AlterSchemaObject):
    """Gives a sequence of a model new settings of the same name - its current number is kept;
    going back restores the old settings.

    Args:
        model_name: The model.
        sequence: The new version.
    """

    object_type: ClassVar[type[SequenceObjectType]] = SequenceObjectType

    def __init__(self, model_name: str, sequence: DatabaseSequence) -> None:
        super().__init__(model_name)
        self.sequence = sequence

    @property
    def schema_object(self) -> Any:
        return self.sequence
