from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING, Any

from hare.sql.terms.base.node import TNode
from hare.sql.terms.base.term import Term

if TYPE_CHECKING:
    pass
from hare.sql.terms.criteria.criterion import Criterion


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
