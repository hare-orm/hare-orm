from __future__ import annotations

from collections.abc import Iterator, Sequence
from typing import TYPE_CHECKING

from hare.sql.terms.term import Term

if TYPE_CHECKING:
    from hare.sql.sql_context import SqlContext
    from hare.sql.terms.node import Node


class GroupingSetsElement(Term):
    """``GROUPING SETS ((a, b), (a), ())`` - the groups of each set of terms, ``()`` for one group of
    every row (SQL:1999).

    Args:
        sets: The sets of grouped terms.
    """

    structure_attributes = ("sets",)

    def __init__(self, sets: Sequence[Sequence[Term]]) -> None:
        super().__init__()
        self.sets = [list(grouping_set) for grouping_set in sets]

    def get_children(self) -> Iterator[Node]:
        for grouping_set in self.sets:
            yield from grouping_set

    def get_sql(self, sql_context: SqlContext) -> str:
        sets_sql = ",".join(
            f"({','.join(term.get_sql(sql_context) for term in grouping_set)})" for grouping_set in self.sets
        )
        return f"GROUPING SETS ({sets_sql})"
