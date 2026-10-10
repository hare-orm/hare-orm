"""Operations on the triggers of a model."""

from __future__ import annotations

from hare.migrations.operations.triggers.add_trigger import AddTrigger
from hare.migrations.operations.triggers.alter_trigger import AlterTrigger
from hare.migrations.operations.triggers.remove_trigger import RemoveTrigger
from hare.migrations.operations.triggers.rename_trigger import RenameTrigger

__all__ = [
    "AddTrigger",
    "AlterTrigger",
    "RemoveTrigger",
    "RenameTrigger",
]
