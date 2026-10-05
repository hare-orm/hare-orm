from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING

from hare.sql.builder_methods import BuilderMethods
from hare.sql.sql_context import SqlContext
from hare.sql.terms.node import TNode
from hare.sql.terms.term import Term

if TYPE_CHECKING:
    from typing import Self

    from hare.sql.builder.tables.table import Table
from hare.sql.terms.criteria.criterion import Criterion


class IsDistinctFromCriterion(Criterion):
    """``<left> IS DISTINCT FROM <right>`` (SQLite: ``IS NOT``) - a NULL-safe inequality: TRUE when the
    two differ, one NULL included, FALSE when both are NULL; never NULL.
    """

    def __init__(self, left: Term, right: Term, alias: str | None = None) -> None:
        super().__init__(alias=alias)
        self.left = left
        self.right = right

    def nodes_(self) -> Iterator[TNode]:
        yield self  # type:ignore[misc]
        yield from self.right.nodes_()
        yield from self.left.nodes_()

    def get_sql(self, sql_context: SqlContext) -> str:
        sql = sql_context.dialect.renderers.get_distinct_from_sql(
            self.left.get_sql(sql_context), self.right.get_sql(sql_context)
        )
        return sql_context.format_alias_sql(sql, self.alias)

    @BuilderMethods.builder
    def replace_table(self, current_table: Table | None, new_table: Table | None) -> Self:
        """Replaces all occurrences of the specified table with the new table.

        Args:
            current_table: The table to be replaced.
            new_table: The table to replace with.

        Returns:
            A copy of the criterion with the tables replaced.
        """
        self.left = self.left.replace_table(current_table, new_table)
        self.right = self.right.replace_table(current_table, new_table)
        return self
