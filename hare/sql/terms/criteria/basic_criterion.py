from __future__ import annotations

from collections.abc import Iterator
from enum import Enum
from typing import TYPE_CHECKING

from hare.sql.builder_methods import BuilderMethods
from hare.sql.enums import Comparator, Equality, JSONOperators
from hare.sql.sql_context import SqlContext
from hare.sql.terms.node import TNode
from hare.sql.terms.term import Term

if TYPE_CHECKING:
    from typing import Self

    from hare.sql.builder.tables.table import Table
from hare.sql.terms.criteria.criterion import Criterion
from hare.sql.terms.values.value_wrapper import ValueWrapper


class BasicCriterion(Criterion):
    """A comparison of two terms - ``left <comparator> right``."""

    @staticmethod
    def always_true() -> BasicCriterion:
        """A condition every row meets - ``1=1``, written as literals."""
        return BasicCriterion(
            Equality.EQ, ValueWrapper(1, allow_parametrize=False), ValueWrapper(1, allow_parametrize=False)
        )

    @staticmethod
    def always_false() -> BasicCriterion:
        """A condition no row meets - ``1=0``, written as literals."""
        return BasicCriterion(
            Equality.EQ, ValueWrapper(1, allow_parametrize=False), ValueWrapper(0, allow_parametrize=False)
        )

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
        self.left = self.left.replace_table(current_table, new_table)
        self.right = self.right.replace_table(current_table, new_table)
        return self

    def get_sql(self, sql_context: SqlContext) -> str:
        sql = f"{self.left.get_sql(sql_context)}{self.comparator}{self.right.get_sql(sql_context)}"
        if sql_context.with_alias:
            return sql_context.format_alias_sql(sql, self.alias)
        return sql
