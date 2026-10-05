from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING

from hare.instrumentation.enums import ChangePayload, RowOperation

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.instrumentation.capture.change_sink import ChangeSink


@dataclasses.dataclass(frozen=True, slots=True)
class CaptureNeeds:
    """What the writes of a captured model read for ``Meta.change_capture`` - worked out once, when
    the model is finalised.

    Attributes:
        sink: The declared sink.
        payload: What a change holds of its rows.
        operations: The operations captured.
        field_names: The captured fields - each written in the model's table.
        columns: Their columns, then the primary key's and the tenant field's - what a write returns
            of a row.
        tenant_field_name: The model's ``Meta.tenant_field`` as a column-backed field, None without
            one.
    """

    sink: ChangeSink
    payload: ChangePayload
    operations: frozenset[RowOperation]
    field_names: tuple[str, ...]
    columns: tuple[str, ...]
    tenant_field_name: str | None

    @property
    def reads_values(self) -> bool:
        """Whether a change holds the values of its row, not only its key."""
        return self.payload is not ChangePayload.KEYS

    @property
    def reads_before(self) -> bool:
        """Whether a change holds its row as it was before the write."""
        return self.payload is ChangePayload.BEFORE_AND_AFTER

    def captures(self, operation: RowOperation) -> bool:
        """Whether writes of an operation are captured.

        Args:
            operation: The operation.

        Returns:
            True when they are.
        """
        return operation in self.operations
