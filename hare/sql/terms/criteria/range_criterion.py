from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from hare.sql.terms.criteria.criterion import Criterion
from hare.sql.terms.node import Node
from hare.sql.terms.term import Term


class RangeCriterion(Criterion):
    """The base of a condition of a term within two bounds."""

    structure_attributes = ("end", "start", "term")

    def __init__(self, term: Term, start: Any, end: Any, alias: str | None = None) -> None:
        super().__init__(alias)
        self.term = term
        self.start = start
        self.end = end

    def get_children(self) -> Iterator[Node]:
        yield self.term
        yield self.start
        yield self.end

    @property
    def is_aggregate(self) -> bool | None:  # type:ignore[override]
        return self.term.is_aggregate
