from typing import Any

from hare.dialects.postgresql.functions.array.array_subscript import ArraySubscript
from hare.sql.context import SqlContext
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
        self.start = ArraySubscript.get_validated_index(start)
        self.end = ArraySubscript.get_validated_index(end)

    def get_sql(self, ctx: SqlContext) -> str:
        array_sql = ArraySubscript.get_array_sql(self.args[0], ctx.copy(with_alias=False))
        return ctx.format_alias_sql(f"{array_sql}[{self.start + 1}:{self.end}]", self.alias)
