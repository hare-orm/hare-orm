from __future__ import annotations

from typing import TYPE_CHECKING

from hare.sql.enums import JoinType
from hare.sql.exceptions import JoinException
from hare.sql.terms.criteria.criterion import Criterion

if TYPE_CHECKING:
    from hare.sql.queries.builder.query_builder import QueryBuilder
    from hare.sql.queries.tables.aliased_query import AliasedQuery
    from hare.sql.queries.tables.selectable import Selectable
from hare.sql.queries.joins.join_on import JoinOn


class Joiner:
    def __init__(
        self,
        query: QueryBuilder,
        item: Selectable | QueryBuilder | AliasedQuery,
        how: JoinType,
        type_label: str,
    ) -> None:
        self.query = query
        self.item = item
        self.how = how
        self.type_label = type_label

    def on(self, criterion: Criterion | None, collate: str | None = None) -> QueryBuilder:
        if criterion is None:
            raise JoinException(
                f"Parameter 'criterion' is required for a {self.type_label} JOIN but was not supplied."
            )

        self.query.do_join(JoinOn(self.item, self.how, criterion, collate))  # type:ignore[arg-type]
        return self.query
