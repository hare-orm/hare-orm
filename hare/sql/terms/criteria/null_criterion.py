from __future__ import annotations

from collections.abc import Iterator

from hare.sql.sql_context import SqlContext
from hare.sql.terms.criteria.criterion import Criterion
from hare.sql.terms.node import Node
from hare.sql.terms.term import Term


class NullCriterion(Criterion):
    """``term IS NULL``."""

    structure_attributes = ("term",)

    def __init__(self, term: Term, alias: str | None = None) -> None:
        super().__init__(alias)
        self.term = term

    def get_children(self) -> Iterator[Node]:
        yield self.term

    def get_sql(self, sql_context: SqlContext) -> str:
        sql = f"{self.term.get_sql(sql_context)} IS NULL"
        return sql_context.format_alias_sql(sql, self.alias)
