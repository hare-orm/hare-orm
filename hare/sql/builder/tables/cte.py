from __future__ import annotations

from typing import TYPE_CHECKING

from hare.sql.terms.term import Term

if TYPE_CHECKING:
    from hare.sql.builder.tables.selectable import Selectable
from hare.sql.builder.tables.aliased_query import AliasedQuery


class Cte(AliasedQuery):
    def __init__(self, name: str, query: Selectable | None = None, *terms: Term) -> None:
        super().__init__(name, query)
        self.query = query
        self.terms = terms
