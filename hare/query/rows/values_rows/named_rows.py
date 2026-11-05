from __future__ import annotations

from collections import namedtuple
from functools import cached_property
from typing import Any, ClassVar

from hare.core.caching.cache import Cache
from hare.query.rows.constants import ROW_CLASS_CACHE_SIZE
from hare.query.rows.values_rows.tuple_rows import TupleRows


class NamedRows(TupleRows):
    """``.values_list(named=True)``: a namedtuple of the selected values - it selects what a
    tuple row does."""

    #: (field names,) -> the namedtuple class of a row selecting them.
    row_classes: ClassVar[Cache[type[tuple[Any, ...]]]] = Cache(ROW_CLASS_CACHE_SIZE, holds_sql=False)

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

    def make_row(self, values: tuple[Any, ...]) -> Any:
        return self.row_class(*values)

    @cached_property
    def row_class(self) -> type[tuple[Any, ...]]:
        """The namedtuple class of this reader's fixed output names, shared by every batch."""
        return self.get_row_class(self.output_names)
