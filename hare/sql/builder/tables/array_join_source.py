from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from hare.sql.builder.tables.selectable import Selectable
from hare.sql.enums import JoinType

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.sql.sql_context import SqlContext
    from hare.sql.terms.term import Term


class ArrayJoinSource(Selectable):
    """The elements of an array joined to their row under ``name`` - ``[LEFT] ARRAY JOIN "t"."tags" AS
    "name"``, with no condition; equal to another of the same name, so it is joined once.

    Args:
        name: The name the elements are read by.
        array: The array.
        left: Whether a row with an empty array is kept, its element the element type's default.
    """

    joined_without_condition: ClassVar[bool] = True  # type: ignore[misc]

    def __init__(self, name: str, array: Term, *, left: bool) -> None:
        super().__init__(name)
        self.array = array
        self.left = left
        self.join_type = JoinType.LEFT_ARRAY if left else JoinType.ARRAY

    def __eq__(self, other: object) -> bool:
        return isinstance(other, ArrayJoinSource) and other.alias == self.alias

    def __hash__(self) -> int:
        return hash((ArrayJoinSource, self.alias))

    def get_sql(self, sql_context: SqlContext) -> str:
        array_sql = self.array.get_sql(sql_context.copy(with_alias=False, subquery=True))
        return f"{array_sql} AS {sql_context.quote_alias(self.alias)}"
