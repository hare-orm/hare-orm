from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from hare.migrations.operations.operation import Operation


@dataclass(frozen=True, slots=True)
class OperationRewrite:
    """A proposed replacement for two operations, separate from permission to reorder them.

    A tuple with one operation replaces the pair; an empty tuple cancels it.
    Longer proposals do not shorten the sequence and are ignored. The optimizer still checks intermediate
    dependencies and compares the resulting state against the actual input state.
    """

    operations: tuple[Operation, ...]
