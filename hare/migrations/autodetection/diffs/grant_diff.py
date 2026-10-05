from __future__ import annotations

from collections.abc import Hashable
from typing import TYPE_CHECKING, Any

from hare.migrations.autodetection.diffs.schema_object_diff import SchemaObjectDiff
from hare.migrations.operations import AddGrant, HareOperation, RemoveGrant
from hare.migrations.operations.schema_objects import GrantObjectType

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.migrations.state.model_state import ModelState


class GrantDiff(SchemaObjectDiff):
    """The grant operations of a model - a grant has no name: one no longer declared is revoked, a
    new one granted."""

    def get_objects(self, model_state: ModelState) -> list[Any]:
        return GrantObjectType.get_objects(model_state)

    def get_signature(self, schema_object: Any) -> Hashable:
        return repr(schema_object)

    def is_unchanged(self, old_object: Any, new_object: Any) -> bool:
        return bool(old_object == new_object)

    def get_add_operation(self, schema_object: Any) -> HareOperation:
        return AddGrant(model_name=self.new_state.name, grant=schema_object)

    def get_remove_operation(self, schema_object: Any) -> HareOperation:
        return RemoveGrant(model_name=self.new_state.name, grant=schema_object)
