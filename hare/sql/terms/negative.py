from __future__ import annotations

from collections.abc import Iterator

from hare.sql.sql_context import SqlContext
from hare.sql.terms.node import Node
from hare.sql.terms.term import Term


class Negative(Term):
    """``-term``."""

    structure_attributes = ("term",)

    def __init__(self, term: Term) -> None:
        super().__init__()
        self.term = term

    @property
    def is_aggregate(self) -> bool | None:  # type:ignore[override]
        return self.term.is_aggregate

    def get_children(self) -> Iterator[Node]:
        yield self.term

    def get_sql(self, sql_context: SqlContext) -> str:
        return f"-{self.term.get_sql(sql_context)}"
