from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

from hare.migrations.operations.schema_objects.alter_schema_object import AlterSchemaObject
from hare.migrations.operations.schema_objects.object_types.policy_object_type import PolicyObjectType

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.ddl.security.policy import Policy


class AlterPolicy(AlterSchemaObject):
    """Changes a row level security policy of a model's table to a new version of the same name -
    its roles and conditions in place, a new command or type by dropping and creating it; going back
    restores the old version.

    Args:
        model_name: The model.
        policy: The new version.
    """

    object_type: ClassVar[type[PolicyObjectType]] = PolicyObjectType

    def __init__(self, model_name: str, policy: Policy) -> None:
        super().__init__(model_name)
        self.policy = policy

    @property
    def schema_object(self) -> Any:
        return self.policy
