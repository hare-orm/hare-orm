from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

from hare.migrations.operations.schema_objects.alter_schema_object import AlterSchemaObject
from hare.migrations.operations.schema_objects.object_types.view_object_type import ViewObjectType

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.ddl.schema_objects.view import View


class AlterView(AlterSchemaObject):
    """Replaces a view of a model with a new version of the same name, granting the model's grants
    on it again; going back restores the old version.

    Args:
        model_name: The model.
        view: The new version.
    """

    object_type: ClassVar[type[ViewObjectType]] = ViewObjectType

    def __init__(self, model_name: str, view: View) -> None:
        super().__init__(model_name)
        self.view = view

    @property
    def schema_object(self) -> Any:
        return self.view
