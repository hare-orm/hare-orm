from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

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

    def is_absent(self, row: Any) -> bool:
        """Whether every raw column of an optional joined row is NULL."""
        return all(row[column.key] is None for column in self.columns)

    def assign(self, instance: Any, row: Any, names: Sequence[str]) -> None:
        """Decode physical columns directly into the corresponding instance attributes.

        Field readers retain their NULL and custom-conversion contracts. Assignment uses
        the selected column order without allocating an intermediate values tuple;
        logical composite groups are assembled separately by ``read()``.
        """
        for name, column in zip(names, self.columns, strict=True):
            value = row[column.key]
            if column.reader is not None:
                value = column.reader(value)
            object.__setattr__(instance, name, value)

    def read(self, row: Any) -> tuple[Any, ...]:
        """Decode selected columns in order, then assemble their logical groups.

        Each field reader receives its raw value once, including NULL. Conversions
        finish before composite groups are assembled; driver rows stay unchanged.
        """
        converted_values: list[Any] = []
        for column in self.columns:
            value = row[column.key]
            if column.reader is not None:
                value = column.reader(value)
            converted_values.append(value)
        values = tuple(converted_values)
        if not self.groups:
            return values
        grouped = []
        position = 0
        for width in self.groups:
            if width == 1:
                grouped.append(values[position])
            else:
                components = values[position : position + width]
                grouped.append(None if all(value is None for value in components) else components)
            position += width
        return tuple(grouped)
