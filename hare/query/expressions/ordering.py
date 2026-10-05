from __future__ import annotations

from hare.exceptions import QueryError
from hare.sql.enums import Order


class Ordering:
    """A field with an explicit direction and, optionally, NULL placement - built with
    ``F("field").asc(...)``/``.desc(...)`` and passed wherever an ordering string is
    (``.order_by()``, ``.latest()``, ``Meta.ordering``, ``Window(order_by=...)``).

    Args:
        field_name: The field, ``related__field`` or annotation name to order by.
        order: The direction and NULL placement.
    """

    __slots__ = ("field_name", "order")

    def __init__(self, field_name: str, order: Order = Order.ASC) -> None:
        self.field_name = field_name
        self.order = order

    @classmethod
    def build(cls, field_name: str, *, is_ascending: bool, nulls_first: bool, nulls_last: bool) -> Ordering:
        """
        Builds an ordering from the ``nulls_first``/``nulls_last`` flags of ``F.asc()``/``F.desc()``.

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
        return cls(field_name, Order.build(is_ascending, nulls_placement))

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Ordering):
            return NotImplemented
        return self.field_name == other.field_name and self.order == other.order

    def __hash__(self) -> int:
        return hash((self.field_name, self.order))

    def __repr__(self) -> str:
        return f"Ordering({self.field_name!r}, {self.order.name})"
