from __future__ import annotations

from typing import TYPE_CHECKING

from hare.sql import SqlContext, Table
from hare.sql.builder_methods import BuilderMethods
from hare.sql.terms.term import Term

if TYPE_CHECKING:
    from collections.abc import Iterator
    from typing import Self

    from hare.sql.terms.node import Node


class OuterAggregateTerm(Term):
    """``(SELECT <aggregate>)`` - an outer query's aggregate referenced from its child query. The
    scalar SELECT keeps it evaluated per group of the outer query on every dialect; for the child
    query it is a constant and never groups it.
    """

    structure_attributes = ("term",)

    is_aggregate = None
    is_subquery = True

    def __init__(self, term: Term) -> None:
        super().__init__()
        self.term = term

    def get_children(self) -> Iterator[Node]:
        yield self.term

    @BuilderMethods.builder
    def replace_table(self, current_table: Table | None, new_table: Table | None) -> Self:
        """Replaces all occurrences of the specified table with the new table.

        Args:
            current_table: The table to be replaced.
            new_table: The table to replace with.

        Returns:
            A copy of the term with the tables replaced.
        """
        self.term = self.term.replace_table(current_table, new_table)
        return self

    def get_sql(self, sql_context: SqlContext) -> str:
        sql = f"(SELECT {self.term.get_sql(sql_context.copy(with_alias=False))})"
        if sql_context.with_alias and self.alias:
            return sql_context.format_alias_sql(sql, self.alias)
        return sql
