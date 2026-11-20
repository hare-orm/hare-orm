from __future__ import annotations

from typing import TYPE_CHECKING

from hare.exceptions import QueryError
from hare.query.expressions.structure.declarations import ExpressionChild
from hare.query.expressions.structure.enums import ArgumentRole
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

    def get_expression_children(self) -> tuple[ExpressionChild, ...]:
        return (ExpressionChild("target", self.target, ArgumentRole.FIELD),)

    def rebuild_expression(self, children: tuple[ExpressionChild, ...]) -> Ordering:
        return type(self)(children[0].value, self.order)

    @staticmethod
    def get_parts(ordering: str | Ordering | Expression, reverse: bool = False) -> tuple[str | Expression, Order]:
        """Splits one ordering item into what it orders by and its direction.

        Args:
            ordering: A ``"field"``/``"-field"`` string, an ``Ordering``
                (``F("field").desc(nulls_last=True)``, ``Lower("name").asc()``) or an expression,
                ascending.
            reverse: Whether to reverse the ordering - direction and any explicit NULL placement.

        Returns:
            The field name - an ``F()``'s too - or the expression, and the direction.

        Raises:
            QueryError: If the item is neither a string, an ``Ordering`` nor an expression.
        """
        from hare.query.expressions.expression import Expression
        from hare.query.expressions.f import F

        field_name: str | Expression
        if isinstance(ordering, Ordering):
            field_name = ordering.target
            order_type = ordering.order
        elif isinstance(ordering, F):
            field_name = ordering.name
            order_type = Order.ASC
        elif isinstance(ordering, Expression):
            field_name = ordering
            order_type = Order.ASC
        elif isinstance(ordering, str):
            if ordering.startswith("-"):
                field_name = ordering[1:]
                order_type = Order.DESC
            else:
                field_name = ordering
                order_type = Order.ASC
        else:
            raise QueryError(
                "An ordering must be a field name string, an expression or an Ordering "
                f"(F('field').asc()/.desc()), got {ordering!r}"
            )

        if reverse:
            order_type = order_type.get_reversed()

        return field_name, order_type

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
