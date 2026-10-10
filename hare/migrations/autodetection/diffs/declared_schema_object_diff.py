from __future__ import annotations

from collections.abc import Hashable
from dataclasses import fields
from typing import TYPE_CHECKING, Any, ClassVar, cast

from hare.migrations.autodetection.diffs.schema_object_diff import SchemaObjectDiff

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.migrations.operations import HareOperation
    from hare.migrations.operations.schema_objects.object_types.schema_object_type import SchemaObjectType
    from hare.migrations.state.model_state import ModelState


class DeclaredSchemaObjectDiff(SchemaObjectDiff):
    """The operations of one type of named object a model declares beside its table - a view, a
    materialized view, a function, a sequence, a policy: one only renamed is renamed, one changed
    under its name is altered in place. A subclass per type names its type and operations."""

    object_type: ClassVar[type[SchemaObjectType]]
    add_operation_class: ClassVar[type[HareOperation]]
    alter_operation_class: ClassVar[type[HareOperation] | None]
    remove_operation_class: ClassVar[type[HareOperation]]
    rename_operation_class: ClassVar[type[HareOperation] | None]

    def get_objects(self, model_state: ModelState) -> list[Any]:
        return self.object_type.get_objects(model_state)

    def get_signature(self, schema_object: Any) -> Hashable:
        return tuple(
            getattr(schema_object, declared_field.name)
            for declared_field in fields(schema_object)
            if declared_field.name != "name"
        )

    def is_unchanged(self, old_object: Any, new_object: Any) -> bool:
        return bool(old_object == new_object)

    def get_add_operation(self, schema_object: Any) -> HareOperation:
        return self.add_operation_class(self.new_state.name, schema_object)  # type: ignore[call-arg]

    def get_remove_operation(self, schema_object: Any) -> HareOperation:
        return self.remove_operation_class(self.new_state.name, schema_object.name)  # type: ignore[call-arg]

    def get_rename_operation(self, old_object: Any, new_object: Any) -> HareOperation:
        rename_operation_class = cast("type[HareOperation]", self.rename_operation_class)
        return rename_operation_class(  # type: ignore[call-arg]
            self.new_state.name, old_object.name, new_object.name
        )

    def get_alter_operation(self, new_object: Any) -> HareOperation | None:
        if self.alter_operation_class is None:
            return None
        return self.alter_operation_class(self.new_state.name, new_object)  # type: ignore[call-arg]
