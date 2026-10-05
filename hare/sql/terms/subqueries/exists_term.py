from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.sql import SqlContext, Table
from hare.sql.builder.queries.correlated_subqueries import CorrelatedSubqueries
from hare.sql.builder_methods import BuilderMethods
from hare.sql.terms.criteria.criterion import Criterion

if TYPE_CHECKING:  # pragma: nocoverage
    from collections.abc import Iterator
    from typing import Self

    from hare.sql.terms.node import TNode


class ExistsTerm(Criterion):
    """EXISTS (<child SELECT>) - the child query's params share one SqlContext/parameterizer with
    the outer one (the same trick Subquery.get_sql above already uses), so bind-parameter numbers
    don't diverge between the outer and the correlated child query."""

    is_subquery = True
    rewrites_own_correlation = True

    def __init__(self, inner_query: Any) -> None:
        super().__init__()
        self.inner_query = inner_query

    def nodes_(self) -> Iterator[TNode]:
        yield self  # type:ignore[misc]
        yield from self.inner_query.nodes_()

    @BuilderMethods.builder
    def replace_table(self, current_table: Table | None, new_table: Table | None) -> Self:
        """Replaces every occurrence of a table with another one, inside the correlated child query
        too.

        Args:
            current_table: The table to be replaced.
            new_table: The table to replace with.

        Returns:
            A copy of the term with the tables replaced.
        """
        self.inner_query = self.inner_query.replace_table(current_table, new_table)
        return self

    def get_sql(self, sql_context: SqlContext) -> str:
        features = sql_context.dialect.features
        if features.supports_correlated_subqueries and not features.rewrites_correlated_exists:
            sql = f"EXISTS {self.inner_query.get_sql(sql_context)}"
        else:
            sql = CorrelatedSubqueries.get_exists_sql(self.inner_query, sql_context)
        if sql_context.with_alias and self.alias:
            return sql_context.format_alias_sql(sql, self.alias)
        return sql
