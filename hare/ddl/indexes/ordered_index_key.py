from __future__ import annotations

from hare.sql.enums import Order
from hare.sql.sql_context import SqlContext
from hare.sql.terms.term import Term


class OrderedIndexKey(Term):
    """An index key with a sort order - ``(expression) DESC NULLS LAST``.

    Args:
        term: The key's expression.
        order: Its direction and NULL placement.
    """

    def __init__(self, term: Term, order: Order) -> None:
        super().__init__()
        self.term = term
        self.order = order

    def get_sql(self, sql_context: SqlContext) -> str:
        return f"({self.term.get_sql(sql_context)}) {self.order}"
