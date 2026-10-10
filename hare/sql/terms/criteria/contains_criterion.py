from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING, Any

from hare.sql.builder_methods import BuilderMethods
from hare.sql.sql_context import SqlContext
from hare.sql.terms.node import TNode
from hare.sql.terms.term import Term

if TYPE_CHECKING:
    from typing import Self

    from hare.sql.builder.tables.table import Table
from hare.sql.terms.criteria.criterion import Criterion


class ContainsCriterion(Criterion):
    def __init__(self, term: Any, container: Term, alias: str | None = None) -> None:
        """A wrapper for an "IN" criterion.

        Wraps two parts: a term, which is checked for membership, and a container, which can be
        a list or subquery.

        Args:
            term: The term to assert membership for within the container.
            container: A list or subquery.
            alias: Optional alias for the term.
        """
        super().__init__(alias)
        self.term = term
        self.container = container
        self._is_negated = False

    def nodes_(self) -> Iterator[TNode]:
        yield self  # type:ignore[misc]
        yield from self.term.nodes_()
        yield from self.container.nodes_()

    @property
    def is_aggregate(self) -> bool | None:  # type:ignore[override]
        return self.term.is_aggregate

    @BuilderMethods.builder
    def replace_table(self, current_table: Table | None, new_table: Table | None) -> Self:
        """Replaces all occurrences of the specified table with the new table.

        Useful when reusing fields across queries.

        Args:
            current_table: The table to be replaced.
            new_table: The table to replace with.

        Returns:
            A copy of the criterion with the tables replaced.
        """
        self.term = self.term.replace_table(current_table, new_table)
        return self

    def get_sql(self, sql_context: SqlContext) -> str:
        container_context = sql_context.copy(subquery=True)
        sql = "{term} {not_}IN {container}".format(
            term=self.term.get_sql(sql_context),
            container=self.container.get_sql(container_context),
            not_="NOT " if self._is_negated else "",
        )
        return sql_context.format_alias_sql(sql, self.alias)

    @BuilderMethods.builder
    def negate(self) -> Self:  # type:ignore[override]
        self._is_negated = True
        return self
