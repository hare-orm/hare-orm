from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING

from hare.sql.context import SqlContext
from hare.sql.terms.base.node import TNode
from hare.sql.terms.base.term import Term
from hare.sql.utils import builder

if TYPE_CHECKING:
    from typing import Self

    from hare.sql.queries.tables.table import Table
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

    def get_sql(self, ctx: SqlContext) -> str:
        sql = f"{self.left.get_sql(ctx)} {ctx.dialect.is_distinct_from_operator} {self.right.get_sql(ctx)}"
        return ctx.format_alias_sql(sql, self.alias)

    @builder
    def replace_table(  # type:ignore[return]
        self, current_table: Table | None, new_table: Table | None
    ) -> Self:
        """Replaces all occurrences of the specified table with the new table.

        Args:
            current_table: The table to be replaced.
            new_table: The table to replace with.

        Returns:
            A copy of the criterion with the tables replaced.
        """
        self.left = self.left.replace_table(current_table, new_table)
        self.right = self.right.replace_table(current_table, new_table)
