from __future__ import annotations

from typing import ClassVar

from hare.migrations.operations.schema_objects.object_types.policy_object_type import PolicyObjectType
from hare.migrations.operations.schema_objects.remove_schema_object import RemoveSchemaObject


class RemovePolicy(RemoveSchemaObject):
    """Drops a row level security policy of a model's table; going back creates it again from the
    state.

    Args:
        model_name: The model.
        name: The policy's name.
    """

    object_type: ClassVar[type[PolicyObjectType]] = PolicyObjectType

    def __init__(self, model_name: str, name: str) -> None:
        super().__init__(model_name, name)
