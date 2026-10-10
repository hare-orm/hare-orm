from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING, Any

from hare.sql.builder_methods import BuilderMethods
from hare.sql.enums import Comparator
from hare.sql.sql_context import SqlContext
from hare.sql.terms.node import TNode
from hare.sql.terms.term import Term

if TYPE_CHECKING:
    from typing import Self

    from hare.sql.builder.tables.table import Table
from hare.sql.terms.criteria.complex_criterion import ComplexCriterion
from hare.sql.terms.criteria.criterion import Criterion


class NestedCriterion(Criterion):
    def __init__(
        self,
        comparator: Comparator,
        nested_comparator: ComplexCriterion,
        left: Any,
        right: Any,
        nested: Any,
        alias: str | None = None,
    ) -> None:
        super().__init__(alias)
        self.left = left
        self.comparator = comparator
        self.nested_comparator = nested_comparator
        self.right = right
        self.nested = nested

    def nodes_(self) -> Iterator[TNode]:
        yield self  # type:ignore[misc]
        yield from self.right.nodes_()
        yield from self.left.nodes_()
        yield from self.nested.nodes_()

    @property
    def is_aggregate(self) -> bool | None:  # type:ignore[override]
        return Term.get_combined_is_aggregate([term.is_aggregate for term in [self.left, self.right, self.nested]])

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
        self.nested = self.nested.replace_table(current_table, new_table)
        return self

    def get_sql(self, sql_context: SqlContext) -> str:
        left = self.left.get_sql(sql_context)
        comparator = self.comparator
        right = self.right.get_sql(sql_context)
        nested_comparator = self.nested_comparator
        nested = self.nested.get_sql(sql_context)
        sql = f"{left}{comparator}{right}{nested_comparator}{nested}"

        if sql_context.with_alias:
            return sql_context.format_alias_sql(sql, self.alias)

        return sql
