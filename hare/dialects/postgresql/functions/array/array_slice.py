from __future__ import annotations

from typing import Any

from hare.dialects.postgresql.functions.array.array_subscript import ArraySubscript
from hare.sql.sql_context import SqlContext
from hare.sql.terms.containers.array_element_term import ArrayElementTerm
from hare.sql.terms.functions.function import Function


class ArraySlice(Function):
    """``array_expr[start + 1:end]`` - the rows of an array from the 0-indexed ``start`` up to,
    not including, ``end``, as Python slices them.

    Args:
        term: The array-valued expression.
        start: The first row, from 0.
        end: The row after the last one.
    """

    def __init__(self, term: Any, start: int, end: int, alias: str | None = None) -> None:
        super().__init__("array_slice", term, alias=alias)
        self.start = ArrayElementTerm.get_validated_index(start)
        self.end = ArrayElementTerm.get_validated_index(end)

    def get_sql(self, sql_context: SqlContext) -> str:
        array_sql = ArraySubscript.get_array_sql(self.args[0], sql_context.copy(with_alias=False))
        return sql_context.format_alias_sql(f"{array_sql}[{self.start + 1}:{self.end}]", self.alias)
