from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

from hare.migrations.operations.schema_objects.object_types.grant_object_type import GrantObjectType
from hare.migrations.operations.schema_objects.remove_schema_object import RemoveSchemaObject

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.ddl.security.grant import Grant
    from hare.migrations.state.state import State


class RemoveGrant(RemoveSchemaObject):
    """Revokes the privileges of a grant of a model - given in full, a grant has no name; going back
    grants them again.

    Args:
        model_name: The model.
        grant: The grant, as the model declared it.
    """

    object_type: ClassVar[type[GrantObjectType]] = GrantObjectType

    def __init__(self, model_name: str, grant: Grant) -> None:
        # Not RemoveSchemaObject's own arguments - a grant is found by itself, not by a name.
        self.model_name = model_name
        self.grant = grant
        self.name = None
        self.fields = None

    def describe(self) -> str:
        return f"Revoke {self.grant.describe()} of {self.model_name}"

    def find(self, state: State, app_label: str) -> Any:
        return self.object_type.find_grant(self.get_model_state(state, app_label, self.model_name), self.grant)
