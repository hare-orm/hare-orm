from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.migrations.operations import Operation


@dataclasses.dataclass(frozen=True, slots=True)
class OperationEffect:
    """What one operation does to the database's data.

    Attributes:
        operation: The operation.
        reversible: Whether it can be unapplied.
        rewrites_table: Whether the database rewrites the whole table - the time it takes grows
            with the table's rows.
        loses_data: Whether values or rows can be lost - a column or a table dropped, a column's
            type changed.
        reason: Why it rewrites the table or loses data; None when it does neither.
    """

    operation: Operation
    reversible: bool
    rewrites_table: bool = False
    loses_data: bool = False
    reason: str | None = None
