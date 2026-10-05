from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING

from hare.sql.terms.node import TNode
from hare.sql.terms.term import Term

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.sql.sql_context import SqlContext


class TsQueryInvert(Term):
    def __init__(self, term: Term) -> None:
        super().__init__()
        self.term = term

    def nodes_(self) -> Iterator[TNode]:
        yield self  # type:ignore[misc]
        yield from self.term.nodes_()

    @property
    def is_aggregate(self) -> bool | None:  # type:ignore[override]
        return self.term.is_aggregate

    def get_sql(self, sql_context: SqlContext) -> str:
        sql = f"!!({self.term.get_sql(sql_context)})"
        if sql_context.with_alias and self.alias:  # pragma: nocoverage
            return f'{sql} "{self.alias}"'
        return sql
