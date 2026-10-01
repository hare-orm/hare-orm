from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.sql import SqlContext, Table
from hare.sql.terms.criteria.criterion import Criterion
from hare.sql.utils import builder

if TYPE_CHECKING:  # pragma: nocoverage
    from collections.abc import Iterator
    from typing import Self

    from hare.sql.terms.base.node import TNode


class ExistsTerm(Criterion):
    """EXISTS (<child SELECT>) - the child query's params share one SqlContext/parameterizer with
    the outer one (the same trick Subquery.get_sql above already uses), so bind-parameter numbers
    don't diverge between the outer and the correlated child query."""

    is_subquery = True

    def __init__(self, inner_query: Any) -> None:
        super().__init__()
        self.inner_query = inner_query

    def nodes_(self) -> Iterator[TNode]:
        yield self  # type:ignore[misc]
        yield from self.inner_query.nodes_()

    @builder
    def replace_table(  # type:ignore[return]
        self, current_table: Table | None, new_table: Table | None
    ) -> Self:
        """Replaces every occurrence of a table with another one, inside the correlated child query
        too.

        Args:
            current_table: The table to be replaced.
            new_table: The table to replace with.

        Returns:
            A copy of the term with the tables replaced.
        """
        self.inner_query = self.inner_query.replace_table(current_table, new_table)

    def get_sql(self, ctx: SqlContext) -> str:
        inner_sql = self.inner_query.get_sql(ctx)
        sql = f"EXISTS {inner_sql}"
        if ctx.with_alias and self.alias:
            return ctx.format_alias_sql(sql, self.alias)
        return sql
