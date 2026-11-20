from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

from hare.migrations.operations.schema_objects.alter_schema_object import AlterSchemaObject
from hare.migrations.operations.schema_objects.object_types.materialized_view_object_type import (
    MaterializedViewObjectType,
)

if TYPE_CHECKING:
    from hare.ddl.schema_objects.materialized_view import MaterializedView


class AlterMaterializedView(AlterSchemaObject):
    """Replaces a model's materialized view according to its dialect, preserving model grants.
    Storage belonging to the view may be rebuilt and refilled; an explicitly declared target
    table keeps its rows. Going back restores the previous declaration.

    Args:
        model_name: The model.
        view: The new version.
    """

    object_type: ClassVar[type[MaterializedViewObjectType]] = MaterializedViewObjectType

    def __init__(self, model_name: str, view: MaterializedView) -> None:
        super().__init__(model_name)
        self.view = view

    @property
    def schema_object(self) -> Any:
        return self.view
