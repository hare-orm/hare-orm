from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

from hare.migrations.operations.schema_objects.add_schema_object import AddSchemaObject
from hare.migrations.operations.schema_objects.object_types.materialized_view_object_type import (
    MaterializedViewObjectType,
)

if TYPE_CHECKING:
    from hare.ddl.schema_objects.materialized_view import MaterializedView


class AddMaterializedView(AddSchemaObject):
    """Creates a materialized view a model declares, with the unique index of its
    ``unique_columns``; going back drops it.

    Args:
        model_name: The model.
        view: The view.
        replacing: The view takes the place of the one of its name the migration drops - a table it
            writes into keeps its rows, not filled again.
    """

    object_type: ClassVar[type[MaterializedViewObjectType]] = MaterializedViewObjectType

    def __init__(self, model_name: str, view: MaterializedView, *, replacing: bool = False) -> None:
        super().__init__(model_name)
        self.view = view
        self.replacing = replacing

    def get_type_options(self) -> dict[str, Any]:
        return {"replacing": self.replacing}

    @property
    def schema_object(self) -> Any:
        return self.view
