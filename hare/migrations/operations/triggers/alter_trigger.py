from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

from hare.migrations.operations.schema_objects.alter_schema_object import AlterSchemaObject
from hare.migrations.operations.schema_objects.trigger_object_type import TriggerObjectType

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.ddl.triggers import Trigger


class AlterTrigger(AlterSchemaObject):
    """Replaces a trigger of a model with a new version of the same name.

    Args:
        model_name: The model.
        trigger: The new version.
    """

    object_type: ClassVar[type[TriggerObjectType]] = TriggerObjectType

    def __init__(self, model_name: str, trigger: Trigger) -> None:
        super().__init__(model_name)
        self.trigger = trigger

    @property
    def schema_object(self) -> Any:
        return self.trigger
