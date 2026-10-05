from __future__ import annotations

from collections import namedtuple
from collections.abc import Sequence
from typing import Any, ClassVar, cast

from hare.core.caching.cache import Cache
from hare.query.rows.constants import ROW_CLASS_CACHE_SIZE
from hare.query.rows.values_rows.tuple_rows import TupleRows
from hare.query.rows.values_rows.values_rows import ColumnConverter


class NamedRows(TupleRows):
    """``.values_list(named=True)``: a namedtuple of the selected values - it selects what a
    tuple row does."""

    #: (field names,) -> the namedtuple class of a row selecting them.
    row_classes: ClassVar[Cache[type[tuple[Any, ...]]]] = Cache(
        ROW_CLASS_CACHE_SIZE, holds_sql=False, keyed_by_model=False
    )

    @staticmethod
    def get_row_class(field_names: tuple[str, ...]) -> type[tuple[Any, ...]]:
        """The namedtuple class of a row.

        Args:
            field_names: The selected names, in output order.

        Returns:
            A namedtuple class; a name that isn't a valid identifier becomes ``_<index>``.
        """
        row_class = NamedRows.row_classes.get((field_names,))
        if row_class is None:
            row_class = NamedRows.row_classes[(field_names,)] = namedtuple("Row", field_names, rename=True)
        return row_class

    def get_row(self, values: tuple[Any, ...]) -> Any:
        return self.get_row_class(self.output_names)(*values)

    def read_with_accelerator(
        self, reader: Any, rows: Sequence[Any], column_converters: list[ColumnConverter]
    ) -> list[Any]:
        return cast("list[Any]", reader.read_named(rows, self.get_row_class(self.output_names)))

    def get_rows(self, result: Sequence[Any], column_keys: list[ColumnConverter]) -> list[Any]:
        if self.has_composite_outputs:
            return super().get_rows(result, column_keys)
        row_class = self.get_row_class(self.output_names)
        return [
            row_class(*(entry[key] if converter is None else converter(entry[key]) for key, converter in column_keys))
            for entry in result
        ]
