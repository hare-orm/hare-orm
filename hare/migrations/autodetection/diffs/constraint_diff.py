from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.ddl.constraints.unique_constraint import UniqueConstraint
from hare.migrations.autodetection.diffs.schema_object_diff import SchemaObjectDiff
from hare.migrations.operations import AddConstraint, HareOperation, RemoveConstraint, RenameConstraint
from hare.migrations.operations.schema_objects.object_types.constraint_object_type import ConstraintObjectType

if TYPE_CHECKING:
    from hare.migrations.state.model_state import ModelState


class ConstraintDiff(SchemaObjectDiff):
    """The constraint operations of a model. A constraint is unchanged only when it is equal in
    everything; an unnamed unique constraint is removed by its fields."""

    def get_objects(self, model_state: ModelState) -> list[Any]:
        return ConstraintObjectType.get_objects(model_state)

    def is_made_by_its_field(self, new_object: Any) -> bool:
        # An unnamed unique constraint on one field that is unique=True itself: both compute the
        # same constraint name from the table and the field, so adding it as well would create
        # the same constraint twice.
        if not (
            isinstance(new_object, UniqueConstraint)
            and new_object.name is None
            and len(new_object.fields) == 1
            and new_object.condition is None
            and not new_object.include
            and not new_object.deferrable
            and new_object.nulls_distinct is None
        ):
            return False
        field = self.new_state.fields.get(new_object.fields[0])
        return bool(getattr(field, "unique", False))

    def get_add_operation(self, schema_object: Any) -> HareOperation:
        return AddConstraint(model_name=self.new_state.name, constraint=schema_object)

    def get_remove_operation(self, schema_object: Any) -> HareOperation:
        if isinstance(schema_object, UniqueConstraint) and schema_object.name is None:
            return RemoveConstraint(model_name=self.new_state.name, fields=list(schema_object.fields))
        return RemoveConstraint(model_name=self.new_state.name, name=schema_object.name)

    def get_rename_operation(self, old_object: Any, new_object: Any) -> HareOperation:
        return RenameConstraint(model_name=self.new_state.name, old_name=old_object.name, new_name=new_object.name)
