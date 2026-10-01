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


class NullCriterion(Criterion):
    def __init__(self, term: Term, alias: str | None = None) -> None:
        super().__init__(alias)
        self.term = term

    def nodes_(self) -> Iterator[TNode]:
        yield self  # type:ignore[misc]
        yield from self.term.nodes_()

    @builder
    def replace_table(  # type:ignore[return]
        self, current_table: Table | None, new_table: Table | None
    ) -> Self:
        """Replaces all occurrences of the specified table with the new table.

        Useful when reusing fields across queries.

        Args:
            current_table: The table to be replaced.
            new_table: The table to replace with.

        Returns:
            A copy of the criterion with the tables replaced.
        """
        self.term = self.term.replace_table(current_table, new_table)

    def get_sql(self, ctx: SqlContext) -> str:
        sql = f"{self.term.get_sql(ctx)} IS NULL"
        return ctx.format_alias_sql(sql, self.alias)
