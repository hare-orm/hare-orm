from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

from hare.migrations.operations.schema_objects.add_schema_object import AddSchemaObject
from hare.migrations.operations.schema_objects.object_types.trigger_object_type import TriggerObjectType

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.ddl.schema_objects.trigger import Trigger


class AddTrigger(AddSchemaObject):
    """Adds a trigger to a model.

    Args:
        model_name: The model.
        trigger: The trigger.
    """

    object_type: ClassVar[type[TriggerObjectType]] = TriggerObjectType

    def __init__(self, model_name: str, trigger: Trigger) -> None:
        super().__init__(model_name)
        self.trigger = trigger

    @property
    def schema_object(self) -> Any:
        return self.trigger
