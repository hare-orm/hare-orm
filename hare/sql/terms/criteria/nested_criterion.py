from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from hare.sql.enums import Comparator
from hare.sql.sql_context import SqlContext
from hare.sql.terms.criteria.complex_criterion import ComplexCriterion
from hare.sql.terms.criteria.criterion import Criterion
from hare.sql.terms.node import Node
from hare.sql.terms.term import Term


class NestedCriterion(Criterion):
    """A comparison whose right side is itself a condition joined to another term."""

    structure_attributes = ("comparator", "left", "nested", "nested_comparator", "right")

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

    def get_children(self) -> Iterator[Node]:
        yield self.right
        yield self.left
        yield self.nested

    @property
    def is_aggregate(self) -> bool | None:  # type:ignore[override]
        return Term.get_combined_is_aggregate([term.is_aggregate for term in [self.left, self.right, self.nested]])

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
