from __future__ import annotations

from collections.abc import Callable
from operator import itemgetter
from typing import Any

from hare.query.enums import RowShape
from hare.query.rows.values_rows.values_rows import ValuesRows


class TupleRows(ValuesRows):
    """``.values_list()``: a tuple of the selected values."""

    plan_shape = RowShape.TUPLE

    @classmethod
    def get_column_aliases(cls, output_name: str, first_column_index: int, column_count: int) -> tuple[str, ...]:
        return tuple(str(first_column_index + index) for index in range(column_count))

    def make_row(self, values: tuple[Any, ...]) -> Any:
        return values

    def get_reader(self, position: int, alias: str) -> Callable[[Any], Any]:
        return itemgetter(position)
