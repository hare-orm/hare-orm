from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

from hare.migrations.operations.schema_objects.alter_schema_object import AlterSchemaObject
from hare.migrations.operations.schema_objects.object_types.materialized_view_object_type import (
    MaterializedViewObjectType,
)

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.ddl.schema_objects.materialized_view import MaterializedView


class AlterMaterializedView(AlterSchemaObject):
    """Replaces a materialized view of a model with a new version of the same name - dropped and
    created again, filled anew, the model's grants on it granted again; going back restores the old
    version.

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
