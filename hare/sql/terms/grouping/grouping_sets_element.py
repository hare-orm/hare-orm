from __future__ import annotations

from collections.abc import Iterator, Sequence
from typing import TYPE_CHECKING

from hare.sql.builder_methods import BuilderMethods
from hare.sql.terms.term import Term

if TYPE_CHECKING:  # pragma: nocoverage
    from typing import Self

    from hare.sql.builder.tables.table import Table
    from hare.sql.sql_context import SqlContext
    from hare.sql.terms.node import TNode


class GroupingSetsElement(Term):
    """``GROUPING SETS ((a, b), (a), ())`` - the groups of each set of terms, ``()`` for one group of
    every row (SQL:1999).

    Args:
        sets: The sets of grouped terms.
    """

    def __init__(self, sets: Sequence[Sequence[Term]]) -> None:
        super().__init__()
        self.sets = [list(grouping_set) for grouping_set in sets]

    def nodes_(self) -> Iterator[TNode]:
        yield self  # type:ignore[misc]
        for grouping_set in self.sets:
            for term in grouping_set:
                yield from term.nodes_()

    def get_sql(self, sql_context: SqlContext) -> str:
        sets_sql = ",".join(
            f"({','.join(term.get_sql(sql_context) for term in grouping_set)})" for grouping_set in self.sets
        )
        return f"GROUPING SETS ({sets_sql})"

    @BuilderMethods.builder
    def replace_table(self, current_table: Table | None, new_table: Table | None) -> Self:
        self.sets = [
            [term.replace_table(current_table, new_table) for term in grouping_set] for grouping_set in self.sets
        ]
        return self
