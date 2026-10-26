from __future__ import annotations

from typing import TYPE_CHECKING

from hare.exceptions import QueryError
from hare.sql.order import Order

if TYPE_CHECKING:
    from hare.query.expressions.expression import Expression


class Ordering:
    """A field or an expression with an explicit direction and, optionally, NULL placement - built
    with ``F("field").asc(...)``/``.desc(...)`` or ``Lower("name").desc(...)`` and passed wherever an
    ordering string is (``.order_by()``, ``.latest()``, ``Meta.ordering``, ``Window(order_by=...)``,
    an index's keys).

    Args:
        target: The field, ``related__field`` or annotation name to order by, or an expression.
        order: The direction and NULL placement.
    """

    __slots__ = ("order", "target")

    def __init__(self, target: str | Expression, order: Order = Order.ASC) -> None:
        self.target = target
        self.order = order

    @classmethod
    def build(cls, target: str | Expression, *, is_ascending: bool, nulls_first: bool, nulls_last: bool) -> Ordering:
        """
        Builds an ordering from the ``nulls_first``/``nulls_last`` flags of ``asc()``/``desc()``.

        Raises:
            QueryError: If both ``nulls_first`` and ``nulls_last`` are set.
        """
        if nulls_first and nulls_last:
            raise QueryError("nulls_first and nulls_last are mutually exclusive")
        nulls_placement: bool | None = None
        if nulls_first:
            nulls_placement = True
        elif nulls_last:
            nulls_placement = False
        return cls(target, Order.build(is_ascending, nulls_placement))

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Ordering):
            return NotImplemented
        return self.target == other.target and self.order == other.order

    def __hash__(self) -> int:
        return hash((self.target, self.order))

    def __repr__(self) -> str:
        return f"Ordering({self.target!r}, {self.order.name})"
