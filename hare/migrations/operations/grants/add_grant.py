from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

from hare.migrations.operations.schema_objects.add_schema_object import AddSchemaObject
from hare.migrations.operations.schema_objects.object_types.grant_object_type import GrantObjectType

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.ddl.security.grant import Grant


class AddGrant(AddSchemaObject):
    """Grants privileges on a model's table or on an object it declares; going back revokes them.

    Args:
        model_name: The model.
        grant: The grant.
    """

    object_type: ClassVar[type[GrantObjectType]] = GrantObjectType

    def __init__(self, model_name: str, grant: Grant) -> None:
        super().__init__(model_name)
        self.grant = grant

    @property
    def schema_object(self) -> Any:
        return self.grant

    def describe(self) -> str:
        return f"Grant {self.grant.describe()} of {self.model_name}"
