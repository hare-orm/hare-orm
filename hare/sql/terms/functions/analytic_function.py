from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.sql.context import SqlContext
from hare.sql.enums import Order
from hare.sql.utils import builder

if TYPE_CHECKING:
    from typing import Self

    from hare.sql.terms.field import Field
from hare.sql.terms.functions.aggregate_function import AggregateFunction


class AnalyticFunction(AggregateFunction):
    is_aggregate = False
    is_analytic = True

    def __init__(self, name: str, *args: Any, **kwargs: Any) -> None:
        super().__init__(name, *args, **kwargs)
        self._filters: list[Any] = []
        self._partition: list[Any] = []
        self._orderbys: list[tuple[Field, Order | None]] = []
        self._include_filter = False
        self._include_over = False

    def __copy__(self) -> Self:
        # AggregateFunction.__copy__ already re-copies _filters; over()/orderby() mutate
        # _partition/_orderbys in place the same way, so they need the same treatment.
        new_term = super().__copy__()
        new_term._partition = list(self._partition)
        new_term._orderbys = list(self._orderbys)
        return new_term

    @builder
    def over(self, *terms: Any) -> Self:  # type:ignore[return]
        self._include_over = True
        self._partition += terms

    @builder
    def orderby(self, *terms: Any, **kwargs: Any) -> Self:  # type:ignore[return]
        self._include_over = True
        self._orderbys += [(term, kwargs.get("order")) for term in terms]

    def _orderby_field(self, field: Field, orient: Order | None, ctx: SqlContext) -> str:
        if orient is None:
            return field.get_sql(ctx)

        return f"{field.get_sql(ctx)} {orient}"

    def get_partition_sql(self, ctx: SqlContext) -> str:
        terms = []
        if self._partition:
            terms.append(
                "PARTITION BY {args}".format(
                    args=",".join(p.get_sql(ctx) if hasattr(p, "get_sql") else str(p) for p in self._partition)
                )
            )

        if self._orderbys:
            terms.append(
                "ORDER BY {orderby}".format(
                    orderby=",".join(self._orderby_field(field, orient, ctx) for field, orient in self._orderbys)
                )
            )

        return " ".join(terms)

    def get_function_sql(self, ctx: SqlContext) -> str:
        function_sql = super().get_function_sql(ctx)
        partition_sql = self.get_partition_sql(ctx)

        sql = function_sql
        if self._include_over:
            sql += f" OVER({partition_sql})"

        return sql
