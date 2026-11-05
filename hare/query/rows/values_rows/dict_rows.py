from __future__ import annotations

from collections.abc import Callable
from operator import itemgetter
from typing import Any

from hare.query.enums import RowShape
from hare.query.rows.constants import COMPOSITE_KEY_COMPONENT_ALIAS_SEPARATOR
from hare.query.rows.values_rows.values_rows import ValuesRows


class DictRows(ValuesRows):
    """``.values()``: a dict by output key."""

    plan_shape = RowShape.DICT
    keyed_by_output_name = True

    @classmethod
    def get_column_aliases(cls, output_name: str, first_column_index: int, column_count: int) -> tuple[str, ...]:
        if column_count == 1:
            return (output_name,)
        return tuple(f"{output_name}{COMPOSITE_KEY_COMPONENT_ALIAS_SEPARATOR}{index}" for index in range(column_count))

    def make_row(self, values: tuple[Any, ...]) -> dict[str, Any]:
        return dict(zip(self.output_names, values, strict=True))

    def get_reader(self, position: int, alias: str) -> Callable[[Any], Any]:
        return itemgetter(alias)
