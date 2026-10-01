"""Operations on the triggers of a model."""

from hare.migrations.operations.triggers.add_trigger import AddTrigger
from hare.migrations.operations.triggers.alter_trigger import AlterTrigger
from hare.migrations.operations.triggers.declarations import RemoveTrigger, RenameTrigger

__all__ = [
    "AddTrigger",
    "AlterTrigger",
    "RemoveTrigger",
    "RenameTrigger",
]
