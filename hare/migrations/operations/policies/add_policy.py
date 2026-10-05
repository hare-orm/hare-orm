from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

from hare.migrations.operations.schema_objects.add_schema_object import AddSchemaObject
from hare.migrations.operations.schema_objects.object_types.policy_object_type import PolicyObjectType

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.ddl.security.policy import Policy


class AddPolicy(AddSchemaObject):
    """Creates a row level security policy on a model's table; going back drops it.

    Args:
        model_name: The model.
        policy: The policy.
    """

    object_type: ClassVar[type[PolicyObjectType]] = PolicyObjectType

    def __init__(self, model_name: str, policy: Policy) -> None:
        super().__init__(model_name)
        self.policy = policy

    @property
    def schema_object(self) -> Any:
        return self.policy
