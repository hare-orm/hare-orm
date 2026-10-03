from __future__ import annotations

from typing import TYPE_CHECKING

from hare.sql.context import SqlContext
from hare.sql.enums import Order
from hare.sql.terms.base.term import Term

if TYPE_CHECKING:
    pass


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

    def get_sql(self, ctx: SqlContext) -> str:
        return f"({self.term.get_sql(ctx)}) {self.order}"
