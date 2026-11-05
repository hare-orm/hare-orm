from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING

from hare.sql.terms.node import Node
from hare.sql.terms.term import Term

if TYPE_CHECKING:
    from hare.sql.sql_context import SqlContext


class TsQueryInvert(Term):
    """``!! query`` - the negation of a text search query."""

    structure_attributes = ("term",)

    def __init__(self, term: Term) -> None:
        super().__init__()
        self.term = term

    def get_children(self) -> Iterator[Node]:
        yield self.term

    @property
    def is_aggregate(self) -> bool | None:  # type:ignore[override]
        return self.term.is_aggregate

    def get_sql(self, sql_context: SqlContext) -> str:
        sql = f"!!({self.term.get_sql(sql_context.copy(with_alias=False))})"
        return sql_context.format_alias_sql(sql, self.alias) if sql_context.with_alias else sql
