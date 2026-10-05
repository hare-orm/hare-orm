from __future__ import annotations

from typing import ClassVar

from hare.migrations.operations.schema_objects.object_types.policy_object_type import PolicyObjectType
from hare.migrations.operations.schema_objects.rename_schema_object import RenameSchemaObject


class RenamePolicy(RenameSchemaObject):
    """Renames a row level security policy of a model's table.

    Args:
        model_name: The model.
        old_name: The old name.
        new_name: The new name.
    """

    object_type: ClassVar[type[PolicyObjectType]] = PolicyObjectType

    def __init__(self, model_name: str, old_name: str, new_name: str) -> None:
        super().__init__(model_name, new_name, old_name=old_name)
