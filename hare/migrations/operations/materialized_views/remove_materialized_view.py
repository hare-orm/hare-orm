from __future__ import annotations

from typing import Any, ClassVar

from hare.migrations.operations.schema_objects.object_types.materialized_view_object_type import (
    MaterializedViewObjectType,
)
from hare.migrations.operations.schema_objects.remove_schema_object import RemoveSchemaObject


class RemoveMaterializedView(RemoveSchemaObject):
    """Drops a materialized view of a model; going back creates it again from the state - a storage of its
    own filled by its query, a table it writes into keeping the rows the drop left in it, not filled again.

    Args:
        model_name: The model.
        name: The view's name.
    """

    object_type: ClassVar[type[MaterializedViewObjectType]] = MaterializedViewObjectType

    def get_type_options(self) -> dict[str, Any]:
        # The drop left a table the view writes into with its rows.
        return {"replacing": True}
