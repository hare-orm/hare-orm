from __future__ import annotations

from collections.abc import Iterator
from enum import Enum
from typing import TYPE_CHECKING

from hare.sql.context import SqlContext
from hare.sql.enums import Comparator, JSONOperators
from hare.sql.terms.base.node import TNode
from hare.sql.terms.base.term import Term
from hare.sql.utils import builder

if TYPE_CHECKING:
    from typing import Self

    from hare.sql.queries.tables.table import Table
from hare.sql.terms.criteria.criterion import Criterion


class BasicCriterion(Criterion):
    def __init__(
        self,
        comparator: Comparator | JSONOperators | Enum,
        left: Term,
        right: Term,
        alias: str | None = None,
    ) -> None:
        """A wrapper for a basic criterion such as equality or inequality.

        Wraps three parts: a left and right term, and a comparator that defines the type of
        comparison.

        Args:
            comparator: The type of comparison, such as "=" or ">".
            left: The term on the left side of the expression.
            right: The term on the right side of the expression.
            alias: Optional alias for the term.
        """
        super().__init__(alias)
        self.comparator = comparator
        self.left = left
        self.right = right

    def nodes_(self) -> Iterator[TNode]:
        yield self  # type:ignore[misc]
        yield from self.right.nodes_()
        yield from self.left.nodes_()

    @property
    def is_aggregate(self) -> bool | None:  # type:ignore[override]
        aggrs = [term.is_aggregate for term in (self.left, self.right)]
        return Term.get_combined_is_aggregate(aggrs)

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
        self.left = self.left.replace_table(current_table, new_table)
        self.right = self.right.replace_table(current_table, new_table)

    def get_sql(self, ctx: SqlContext) -> str:
        sql = f"{self.left.get_sql(ctx)}{self.comparator}{self.right.get_sql(ctx)}"
        if ctx.with_alias:
            return ctx.format_alias_sql(sql, self.alias)
        return sql
