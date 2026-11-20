from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from hare.migrations.operations.dependencies.operation_dependency import OperationDependency
from hare.migrations.operations.enums import DependencyType
from hare.migrations.operations.schema_objects.object_types.sequence_object_type import SequenceObjectType
from hare.migrations.operations.schema_objects.remove_schema_object import RemoveSchemaObject

if TYPE_CHECKING:
    from hare.migrations.state.state import State


class RemoveSequence(RemoveSchemaObject):
    """Drops a sequence of a model; going back creates it again from the state - starting over.

    Args:
        model_name: The model.
        name: The sequence's name.
    """

    object_type: ClassVar[type[SequenceObjectType]] = SequenceObjectType
    name: str

    def __init__(self, model_name: str, name: str) -> None:
        super().__init__(model_name, name)

    def get_provided_dependencies(self, app_label: str, state: State) -> list[OperationDependency]:
        return [OperationDependency.of_schema_object(app_label, self.model_name, self.name, DependencyType.REMOVED)]
