from __future__ import annotations

from collections.abc import Callable
from typing import Any

from hare.query.enums import RowShape
from hare.query.rows.values_rows.tuple_rows import TupleRows


class FlatRows(TupleRows):
    """``.values_list(flat=True)``: the one selected value alone."""

    plan_shape = RowShape.FLAT

    def get_reader(self, position: int, alias: str) -> Callable[[Any], Any]:
        return lambda row: row
