from __future__ import annotations

from collections.abc import Callable
from typing import Any, ClassVar

from clickhouse_driver.columns.tuplecolumn import TupleColumn


class ClickhouseDriverTupleColumns:
    """The writing of a ``Tuple(...)`` column by clickhouse-driver, made to write a column of no
    tuples - the tuples of arrays or maps that are all empty: the library turns its tuples into a
    column per element and then reads each by its position, of which there is none."""

    #: The library's own writing of a tuple column - kept to tell it was replaced.
    library_writers: ClassVar[list[Callable[..., Any]]] = []

    @classmethod
    def install(cls) -> None:
        """Replaces the library's writing of a tuple column - once."""
        if cls.library_writers:
            return
        cls.library_writers.append(TupleColumn.write_data)
        TupleColumn.write_data = cls.write_data

    @staticmethod
    def write_data(column: Any, items: Any, buf: Any) -> None:
        """Writes the tuples of a column - none as no value of each element.

        Args:
            column: The library's tuple column.
            items: The tuples.
            buf: The buffer written into.
        """
        if items:
            ClickhouseDriverTupleColumns.library_writers[0](column, items, buf)
            return
        for nested_column in column.nested_columns:
            nested_column.write_data([], buf)
