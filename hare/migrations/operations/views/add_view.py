from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

from hare.migrations.operations.schema_objects.add_schema_object import AddSchemaObject
from hare.migrations.operations.schema_objects.object_types.view_object_type import ViewObjectType

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.ddl.schema_objects.view import View


class AddView(AddSchemaObject):
    """Creates a view a model declares; going back drops it. A queryset is written as the SQL of
    the connection the migration runs on.

    Args:
        model_name: The model.
        view: The view.
    """

    object_type: ClassVar[type[ViewObjectType]] = ViewObjectType

    def __init__(self, model_name: str, view: View) -> None:
        super().__init__(model_name)
        self.view = view

    @property
    def schema_object(self) -> Any:
        return self.view
