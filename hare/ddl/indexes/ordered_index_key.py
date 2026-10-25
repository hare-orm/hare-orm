from __future__ import annotations

from typing import TYPE_CHECKING

from hare.sql.order import Order
from hare.sql.sql_context import SqlContext
from hare.sql.terms.term import Term

if TYPE_CHECKING:
    from hare.ddl.raw_sql_term import RawSQLTerm


class OrderedIndexKey(Term):
    """An index key with a sort order - ``(expression) DESC NULLS LAST``.

    Args:
        term: The key's expression - a raw SQL one too.
        order: Its direction and NULL placement.
    """

    def __init__(self, term: Term | RawSQLTerm, order: Order) -> None:
        super().__init__()
        self.term = term
        self.order = order

    def get_sql(self, sql_context: SqlContext) -> str:
        return f"({self.term.get_sql(sql_context)}) {self.order}"
