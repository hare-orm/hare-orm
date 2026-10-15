from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field as dataclass_field
from itertools import chain
from typing import Any

from hare.fields.field import Field
from hare.sql import Table
from hare.sql.enums import Order
from hare.sql.terms.criteria.criterion import Criterion
from hare.sql.terms.term import Term

#: One JOIN: the table joined and the ON criterion.
TableCriterionTuple = tuple[Table, Criterion]


@dataclass
class ExpressionResult:
    term: Term
    joins: list[TableCriterionTuple] = dataclass_field(default_factory=list)
    output_field: Field[Any] | None = None
    #: The ``FILTER (WHERE ...)`` condition of an aggregate's resolved argument (``_filter=``).
    aggregate_filter: Criterion | None = None
    #: The order an aggregate reads its rows in (``order_by=``), each term with its direction.
    aggregate_orderings: list[tuple[Term, Order | None]] = dataclass_field(default_factory=list)

    @staticmethod
    def dedup_joins(*joins: Iterable[TableCriterionTuple]) -> list[TableCriterionTuple]:
        """Merges any number of join-tuple collections into one deduplicated list, keeping the
        order they are given in - a JOIN can read the table of an earlier one.

        Args:
            joins: The collections of ``(table, criterion)`` joins.

        Returns:
            The joins, each once.
        """
        return list(dict.fromkeys(chain.from_iterable(joins)))
