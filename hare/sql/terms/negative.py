from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING

from hare.sql.builder_methods import BuilderMethods
from hare.sql.sql_context import SqlContext
from hare.sql.terms.node import TNode

if TYPE_CHECKING:
    from typing import Self

    from hare.sql.builder.tables.table import Table
from hare.sql.terms.term import Term


class Negative(Term):
    def __init__(self, term: Term) -> None:
        super().__init__()
        self.term = term

    @property
    def is_aggregate(self) -> bool | None:  # type:ignore[override]
        return self.term.is_aggregate

    def nodes_(self) -> Iterator[TNode]:
        yield self  # type:ignore[misc]
        yield from self.term.nodes_()

    @BuilderMethods.builder
    def replace_table(self, current_table: Table | None, new_table: Table | None) -> Self:
        """Replaces all occurrences of the specified table with the new table.

        Useful when reusing fields across queries.

        Args:
            current_table: The table to be replaced.
            new_table: The table to replace with.

        Returns:
            A copy of the term with the tables replaced.
        """
        self.term = self.term.replace_table(current_table, new_table)
        return self

    def get_sql(self, sql_context: SqlContext) -> str:
        return f"-{self.term.get_sql(sql_context)}"
