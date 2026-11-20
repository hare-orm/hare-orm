from __future__ import annotations

from typing import ClassVar

from hare.migrations.autodetection.diffs.view_diff import ViewDiff
from hare.migrations.operations import (
    AddMaterializedView,
    AlterMaterializedView,
    HareOperation,
    RemoveMaterializedView,
    RenameMaterializedView,
)
from hare.migrations.operations.schema_objects.object_types.materialized_view_object_type import (
    MaterializedViewObjectType,
)
from hare.migrations.operations.schema_objects.object_types.schema_object_type import SchemaObjectType


class MaterializedViewDiff(ViewDiff):
    """The materialized view operations of a model - a view removed and added under one name replaces
    itself: a table it writes into keeps its rows."""

    object_type: ClassVar[type[SchemaObjectType]] = MaterializedViewObjectType
    add_operation_class: ClassVar[type[HareOperation]] = AddMaterializedView
    alter_operation_class: ClassVar[type[HareOperation] | None] = AlterMaterializedView
    remove_operation_class: ClassVar[type[HareOperation]] = RemoveMaterializedView
    rename_operation_class: ClassVar[type[HareOperation] | None] = RenameMaterializedView

    def get_operations(self) -> list[HareOperation]:
        operations = super().get_operations()
        removed_names = {operation.name for operation in operations if isinstance(operation, RemoveMaterializedView)}
        for operation in operations:
            if isinstance(operation, AddMaterializedView) and operation.view.name in removed_names:
                operation.replacing = True
        return operations
