from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

from hare.migrations.operations.schema_objects.add_schema_object import AddSchemaObject
from hare.migrations.operations.schema_objects.object_types.function_object_type import FunctionObjectType

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.ddl.schema_objects.database_function import DatabaseFunction


class AddFunction(AddSchemaObject):
    """Creates a function a model declares; going back drops it.

    Args:
        model_name: The model.
        function: The function.
    """

    object_type: ClassVar[type[FunctionObjectType]] = FunctionObjectType

    def __init__(self, model_name: str, function: DatabaseFunction) -> None:
        super().__init__(model_name)
        self.function = function

    @property
    def schema_object(self) -> Any:
        return self.function
