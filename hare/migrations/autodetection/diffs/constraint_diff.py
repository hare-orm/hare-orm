from __future__ import annotations

from collections.abc import Hashable
from typing import TYPE_CHECKING, Any, cast

from hare.ddl.constraints.check_constraint import CheckConstraint
from hare.ddl.constraints.exclusion_constraint import ExclusionConstraint
from hare.ddl.constraints.unique_constraint import UniqueConstraint
from hare.migrations.autodetection.diffs.schema_object_diff import SchemaObjectDiff
from hare.migrations.autodetection.state_signatures import StateSignatures
from hare.migrations.operations import AddConstraint, HareOperation, RemoveConstraint, RenameConstraint
from hare.models.enums import ModelOption

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.migrations.state.model_state import ModelState


class ConstraintDiff(SchemaObjectDiff):
    """The constraint operations of a model. A constraint is unchanged only when it is equal in
    everything; an unnamed unique constraint is removed by its fields."""

    def get_objects(self, model_state: ModelState) -> list[Any]:
        return StateSignatures.normalize_constraints(model_state.get_option_list(ModelOption.CONSTRAINTS))

    def get_signature(self, schema_object: Any) -> Hashable:
        if isinstance(schema_object, UniqueConstraint):
            return (
                UniqueConstraint,
                tuple(schema_object.fields),
                schema_object.condition,
                schema_object.deferrable,
                schema_object.initially_deferred,
                schema_object.include,
                schema_object.nulls_distinct,
            )
        if isinstance(schema_object, CheckConstraint):
            return (CheckConstraint, schema_object.check)
        exclusion = cast("ExclusionConstraint", schema_object)
        return (ExclusionConstraint, exclusion.expressions, exclusion.using, exclusion.condition)

    def is_unchanged(self, old_object: Any, new_object: Any) -> bool:
        return bool(old_object == new_object)

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
