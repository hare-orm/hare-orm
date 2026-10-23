from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar, cast

from hare.migrations.autodetection.diffs.declared_schema_object_diff import DeclaredSchemaObjectDiff
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

if TYPE_CHECKING:
    from hare.ddl.schema_objects.view import View


class MaterializedViewDiff(DeclaredSchemaObjectDiff):
    """The materialized view operations of a model - a view removed and added under one name replaces
    itself: a table it writes into keeps its rows."""

    object_type: ClassVar[type[SchemaObjectType]] = MaterializedViewObjectType
    add_operation_class: ClassVar[type[HareOperation]] = AddMaterializedView
    alter_operation_class: ClassVar[type[HareOperation] | None] = AlterMaterializedView
    remove_operation_class: ClassVar[type[HareOperation]] = RemoveMaterializedView
    rename_operation_class: ClassVar[type[HareOperation] | None] = RenameMaterializedView

    def get_read_columns(self, schema_object: Any) -> tuple[tuple[str, str], ...]:
        return cast("View", schema_object).read_columns

    def get_operations(self) -> list[HareOperation]:
        operations = super().get_operations()
        removed_names = {operation.name for operation in operations if isinstance(operation, RemoveMaterializedView)}
        added_names = {operation.view.name for operation in operations if isinstance(operation, AddMaterializedView)}
        for operation in operations:
            if (isinstance(operation, RemoveMaterializedView) and operation.name in added_names) or (
                isinstance(operation, AddMaterializedView) and operation.view.name in removed_names
            ):
                operation.replacing = True
        return operations
