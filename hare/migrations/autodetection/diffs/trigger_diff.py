from __future__ import annotations

from collections.abc import Hashable
from typing import TYPE_CHECKING, Any

from hare.migrations.autodetection.diffs.schema_object_diff import SchemaObjectDiff
from hare.migrations.autodetection.state_signatures import StateSignatures
from hare.migrations.operations import AddTrigger, AlterTrigger, HareOperation, RemoveTrigger, RenameTrigger
from hare.models.enums import ModelOption

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.migrations.state.model_state import ModelState


class TriggerDiff(SchemaObjectDiff):
    """The trigger operations of a model. A trigger changed under the same name is altered in
    place."""

    def get_objects(self, model_state: ModelState) -> list[Any]:
        return StateSignatures.normalize_triggers(model_state.get_option_list(ModelOption.TRIGGERS))

    def get_signature(self, schema_object: Any) -> Hashable:
        return StateSignatures.get_trigger_signature(schema_object)

    def is_unchanged(self, old_object: Any, new_object: Any) -> bool:
        return bool(old_object == new_object)

    def get_add_operation(self, schema_object: Any) -> HareOperation:
        return AddTrigger(model_name=self.new_state.name, trigger=schema_object)

    def get_remove_operation(self, schema_object: Any) -> HareOperation:
        return RemoveTrigger(model_name=self.new_state.name, name=schema_object.name)

    def get_rename_operation(self, old_object: Any, new_object: Any) -> HareOperation:
        return RenameTrigger(model_name=self.new_state.name, old_name=old_object.name, new_name=new_object.name)

    def get_alter_operation(self, new_object: Any) -> HareOperation | None:
        return AlterTrigger(model_name=self.new_state.name, trigger=new_object)
