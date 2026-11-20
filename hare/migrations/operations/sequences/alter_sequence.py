from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

from hare.migrations.operations.dependencies.operation_dependency import OperationDependency
from hare.migrations.operations.enums import DependencyType
from hare.migrations.operations.schema_objects.alter_schema_object import AlterSchemaObject
from hare.migrations.operations.schema_objects.object_types.sequence_object_type import SequenceObjectType

if TYPE_CHECKING:
    from hare.ddl.schema_objects.database_sequence import DatabaseSequence
    from hare.migrations.state.state import State


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

    def get_provided_dependencies(self, app_label: str, state: State) -> list[OperationDependency]:
        return [
            OperationDependency.of_schema_object(
                app_label, self.model_name, self.sequence.name, DependencyType.CHANGED
            )
        ]

    def get_required_dependencies(self, app_label: str, state: State) -> list[OperationDependency]:
        if self.sequence.owned_by is None:
            return []
        return [
            OperationDependency.of_field(app_label, self.model_name, self.sequence.owned_by, DependencyType.CREATED)
        ]
