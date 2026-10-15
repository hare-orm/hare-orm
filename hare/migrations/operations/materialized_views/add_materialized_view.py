from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

from hare.migrations.operations.schema_objects.add_schema_object import AddSchemaObject
from hare.migrations.operations.schema_objects.object_types.materialized_view_object_type import (
    MaterializedViewObjectType,
)

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.ddl.schema_objects.materialized_view import MaterializedView


class AddMaterializedView(AddSchemaObject):
    """Creates a materialized view a model declares, with the unique index of its
    ``unique_columns``; going back drops it.

    Args:
        model_name: The model.
        view: The view.
    """

    object_type: ClassVar[type[MaterializedViewObjectType]] = MaterializedViewObjectType

    def __init__(self, model_name: str, view: MaterializedView) -> None:
        super().__init__(model_name)
        self.view = view

    @property
    def schema_object(self) -> Any:
        return self.view
