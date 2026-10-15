from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from hare.sql.terms.criteria.criterion import Criterion
from hare.sql.terms.node import TNode
from hare.sql.terms.term import Term


class RangeCriterion(Criterion):
    def __init__(self, term: Term, start: Any, end: Any, alias: str | None = None) -> None:
        super().__init__(alias)
        self.term = term
        self.start = start
        self.end = end

    def nodes_(self) -> Iterator[TNode]:
        yield self  # type:ignore[misc]
        yield from self.term.nodes_()
        yield from self.start.nodes_()
        yield from self.end.nodes_()

    @property
    def is_aggregate(self) -> bool | None:  # type:ignore[override]
        return self.term.is_aggregate
