from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.migrations.operations import Operation


@dataclasses.dataclass(frozen=True, slots=True)
class OperationPlan:
    """The operations that bring one app's models from one state to another.

    Attributes:
        operations: The operations, in order.
        warnings: What the change does that needs attention - the same as ``makemigrations``
            prints.
        data_loss_warnings: What the change can lose - a narrowed column, a dropped field.
    """

    operations: tuple[Operation, ...]
    warnings: tuple[str, ...] = ()
    data_loss_warnings: tuple[str, ...] = ()
