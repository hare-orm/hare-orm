from __future__ import annotations

from collections.abc import Iterator
from enum import Enum
from typing import ClassVar

from hare.sql.enums import Comparator, Equality
from hare.sql.sql_context import SqlContext
from hare.sql.terms.criteria.criterion import Criterion
from hare.sql.terms.node import Node
from hare.sql.terms.term import Term
from hare.sql.terms.values.value_wrapper import ValueWrapper


class BasicCriterion(Criterion):
    """A comparison of two terms - ``left <comparator> right``."""

    structure_attributes: ClassVar[tuple[str, ...]] = ("comparator", "left", "right")

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
        comparator: Comparator | Enum,
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

    def get_children(self) -> Iterator[Node]:
        yield self.right
        yield self.left

    @property
    def is_aggregate(self) -> bool | None:  # type:ignore[override]
        aggrs = [term.is_aggregate for term in (self.left, self.right)]
        return Term.get_combined_is_aggregate(aggrs)

    def get_sql(self, sql_context: SqlContext) -> str:
        sql = f"{self.left.get_sql(sql_context)}{self.comparator}{self.right.get_sql(sql_context)}"
        if sql_context.with_alias:
            return sql_context.format_alias_sql(sql, self.alias)
        return sql
