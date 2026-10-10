from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from hare.query.enums import RowShape
from hare.query.rows.values_rows.tuple_rows import TupleRows
from hare.query.rows.values_rows.values_rows import ColumnConverter


class FlatRows(TupleRows):
    """``.values_list(flat=True)``: the one selected value alone."""

    plan_shape = RowShape.FLAT

    def get_row(self, values: tuple[Any, ...]) -> Any:
        return values[0]

    def get_rows(self, result: Sequence[Any], column_keys: list[ColumnConverter]) -> list[Any]:
        if self.has_composite_outputs:
            return super().get_rows(result, column_keys)
        key, converter = column_keys[0]
        if converter is None:
            return [entry[key] for entry in result]
        return [converter(entry[key]) for entry in result]

    def read_with_accelerator(
        self, reader: Any, rows: Sequence[Any], column_converters: list[ColumnConverter]
    ) -> list[Any]:
        return reader.read_flat(rows)

    def get_reader(self, position: int, alias: str) -> Callable[[Any], Any]:
        return lambda row: row
