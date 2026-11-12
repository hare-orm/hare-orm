from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from hare.native.native_modules import NativeModules
from hare.query.enums import RowShape
from hare.query.rows.row_column import RowColumn


@dataclass(frozen=True, slots=True)
class RowLayout:
    """Ordered columns and the groups forming a result's logical values.

    Columns are selected explicitly, so unrelated driver columns never enter the
    result. Multi-column groups represent composite keys: an all-NULL group is
    absent, while a partially NULL group retains all its components. Without
    groups, each column is one value. Model and values readers share this decoder.
    """

    columns: tuple[RowColumn, ...]
    groups: tuple[int, ...] = ()
    _native_layout: Any = field(default=None, init=False, repr=False, compare=False)

    @property
    def native_layout(self) -> Any:
        """The native decoder of these immutable columns, prepared on first values read.

        Model readers use their own model layout and need no values decoder. A values
        plan retains this decoder across executions; it contains no execution inputs.
        """
        if self._native_layout is None:
            object.__setattr__(self, "_native_layout", NativeModules.rows.ValueLayout(self))
        return self._native_layout

    def read(self, row: Any) -> tuple[Any, ...]:
        """Decode selected columns in order, then assemble their logical groups.

        Each field reader receives its raw value once, including NULL. Conversions
        finish before composite groups are assembled; driver rows stay unchanged.
        """
        return NativeModules.rows.read_row_values(row, self)

    def read_many(self, rows: Any) -> list[tuple[Any, ...]]:
        """Decode rows in order with the same column callbacks and NULL grouping as read()."""
        return NativeModules.rows.read_values_batch(rows, self, RowShape.TUPLE, (), None)
