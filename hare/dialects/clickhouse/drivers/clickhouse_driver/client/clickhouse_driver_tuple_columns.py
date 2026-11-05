from __future__ import annotations

from typing import Any

from clickhouse_driver.columns.tuplecolumn import TupleColumn


class ClickhouseDriverTupleColumns:
    """The writing of a ``Tuple(...)`` column by clickhouse-driver, made to write a column of no
    tuples - the tuples of arrays or maps that are all empty: the library turns its tuples into a
    column per element and then reads each by its position, of which there is none."""

    @staticmethod
    def write_data(column: Any, items: Any, buf: Any) -> None:
        """Writes the tuples of a column - none as no value of each element.

        Args:
            column: The library's tuple column.
            items: The tuples.
            buf: The buffer written into.
        """
        if items:
            TupleColumn.write_data(column, items, buf)
            return
        for nested_column in column.nested_columns:
            nested_column.write_data([], buf)
