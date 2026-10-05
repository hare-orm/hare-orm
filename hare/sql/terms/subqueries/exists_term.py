from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.sql import SqlContext, Table
from hare.sql.builder.queries.correlated_subqueries import CorrelatedSubqueries
from hare.sql.builder.queries.query_builder import QueryBuilder
from hare.sql.builder.queries.set_operation_query import SetOperationQuery
from hare.sql.builder_methods import BuilderMethods
from hare.sql.constants import UNION_SET_OPERATIONS
from hare.sql.terms.criteria.criterion import Criterion

if TYPE_CHECKING:
    from collections.abc import Iterator
    from typing import Self

    from hare.sql.terms.node import TNode


class ExistsTerm(Criterion):
    """EXISTS (<child SELECT>) - the child query's params share one SqlContext/parameterizer with
    the outer one, as a ``Subquery``'s do, so bind-parameter numbers
    don't diverge between the outer and the correlated child query."""

    is_subquery = True
    rewrites_inner_correlation = True

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

    def get_union_parts(self) -> list[QueryBuilder] | None:
        """The queries the subquery is a union of - read through the derived tables selecting all of
        its rows (``SELECT 1 FROM (<union>)``); None for a query of any other set operation, or sliced
        as a whole.

        Returns:
            The queries.
        """
        inner_query = self.inner_query
        while ExistsTerm.reads_all_rows_of_one_query(inner_query):
            inner_query = inner_query._from[0]
        if (
            not isinstance(inner_query, SetOperationQuery)
            or inner_query._limit is not None
            or inner_query._offset is not None
            or any(operation not in UNION_SET_OPERATIONS for operation, _query in inner_query._set_operation)
        ):
            return None
        parts = [inner_query.base_query, *(query for _operation, query in inner_query._set_operation)]
        return parts if all(isinstance(part, QueryBuilder) for part in parts) else None

    @staticmethod
    def reads_all_rows_of_one_query(query: Any) -> bool:
        """Whether a query has a row exactly where the one query it reads from has one.

        Args:
            query: The query.

        Returns:
            Whether it does - it reads one derived table, with no condition, join, grouping, offset
            or ``DISTINCT``; a limit of one row or more doesn't change whether a row exists.
        """
        if not isinstance(query, QueryBuilder):
            return False
        limit = query._limit
        return (
            len(query._from) == 1
            and isinstance(query._from[0], (QueryBuilder, SetOperationQuery))
            and query._wheres is None
            and not query._joins
            and not query._groupbys
            and query._havings is None
            and (limit is None or (isinstance(limit.value, int) and limit.value >= 1))
            and query._offset is None
            and not query._distinct
        )

    def get_sql(self, sql_context: SqlContext) -> str:
        features = sql_context.dialect.features
        if features.supports_correlated_subqueries and not features.rewrites_correlated_exists:
            sql = f"EXISTS {self.inner_query.get_sql(sql_context)}"
        elif (union_parts := self.get_union_parts()) is not None:
            # A row exists in a union where it exists in one of its parts - each rewritten alone.
            sql = (
                "(" + " OR ".join(CorrelatedSubqueries.get_exists_sql(part, sql_context) for part in union_parts) + ")"
            )
        else:
            sql = CorrelatedSubqueries.get_exists_sql(self.inner_query, sql_context)
        if sql_context.with_alias and self.alias:
            return sql_context.format_alias_sql(sql, self.alias)
        return sql
