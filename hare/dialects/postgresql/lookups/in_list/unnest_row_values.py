from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING

from hare.sql.terms.term import Term

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.sql import SqlContext
    from hare.sql.terms.node import TNode


class UnnestRowValues(Term):
    """``(SELECT * FROM unnest($1::t1[], $2::t2[], ...))`` - value rows bound as one array
    parameter per column.

    Args:
        column_arrays: One array term per column, cast to that column's array type.
    """

    def __init__(self, column_arrays: list[Term]) -> None:
        super().__init__()
        self.column_arrays = column_arrays

    def nodes_(self) -> Iterator[TNode]:
        yield self  # type:ignore[misc]
        for column_array in self.column_arrays:
            yield from column_array.nodes_()

    def get_sql(self, sql_context: SqlContext) -> str:
        arrays_sql = ", ".join(column_array.get_sql(sql_context) for column_array in self.column_arrays)
        return f"(SELECT * FROM unnest({arrays_sql}))"  # nosec B608 - rendered terms, values are bound
