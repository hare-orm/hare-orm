from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

from hare.migrations.operations.schema_objects.alter_schema_object import AlterSchemaObject
from hare.migrations.operations.schema_objects.object_types.function_object_type import FunctionObjectType

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.ddl.schema_objects.database_function import DatabaseFunction


class AlterFunction(AlterSchemaObject):
    """Replaces a function of a model with a new version of the same name - in place when its
    arguments and result type stay, else dropped and created again with the model's grants on it;
    going back restores the old version.

    Args:
        model_name: The model.
        function: The new version.
    """

    object_type: ClassVar[type[FunctionObjectType]] = FunctionObjectType

    def __init__(self, model_name: str, function: DatabaseFunction) -> None:
        super().__init__(model_name)
        self.function = function

    @property
    def schema_object(self) -> Any:
        return self.function
