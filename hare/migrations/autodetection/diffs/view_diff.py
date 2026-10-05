from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar, cast

from hare.migrations.autodetection.diffs.declared_schema_object_diff import DeclaredSchemaObjectDiff
from hare.migrations.operations import AddView, AlterView, HareOperation, RemoveView, RenameView
from hare.migrations.operations.schema_objects.object_types.schema_object_type import SchemaObjectType
from hare.migrations.operations.schema_objects.object_types.view_object_type import ViewObjectType

if TYPE_CHECKING:
    from hare.ddl.schema_objects.view import View


class ViewDiff(DeclaredSchemaObjectDiff):
    """The view operations of a model."""

    object_type: ClassVar[type[SchemaObjectType]] = ViewObjectType
    add_operation_class: ClassVar[type[HareOperation]] = AddView
    alter_operation_class: ClassVar[type[HareOperation] | None] = AlterView
    remove_operation_class: ClassVar[type[HareOperation]] = RemoveView
    rename_operation_class: ClassVar[type[HareOperation] | None] = RenameView

    def get_read_columns(self, schema_object: Any) -> tuple[tuple[str, str], ...]:
        return cast("View", schema_object).read_columns
