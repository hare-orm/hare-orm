from __future__ import annotations

from typing import TYPE_CHECKING

from hare.sql.terms.base.term import Term

if TYPE_CHECKING:
    from hare.sql.queries.builder.query_builder import QueryBuilder
from hare.sql.queries.tables.aliased_query import AliasedQuery


class Cte(AliasedQuery):
    def __init__(self, name: str, query: QueryBuilder | None = None, *terms: Term) -> None:
        super().__init__(name, query)
        self.query = query
        self.terms = terms
