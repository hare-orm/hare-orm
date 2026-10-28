from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.sql.builder_methods import BuilderMethods
from hare.sql.order import Order
from hare.sql.sql_context import SqlContext
from hare.sql.terms.node import Node, TNode

if TYPE_CHECKING:
    from collections.abc import Iterator
    from typing import Self

    from hare.sql.terms.field import Field
from hare.sql.terms.functions.aggregate_function import AggregateFunction


class AnalyticFunction(AggregateFunction):
    """A window function - ``name(...) OVER (PARTITION BY ... ORDER BY ...)``."""

    is_aggregate = False
    is_analytic = True
    #: Whether the column type of the value it reads holds NULL - a dialect making the value nullable to
    #: give NULL past the partition's edge leaves one that doesn't as it is.
    value_holds_null = True
    #: The field of the value it reads, None for a value of no field - a dialect reads how its type holds
    #: NULL off it.
    value_field: Any = None

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

    def nodes_(self) -> Iterator[TNode]:
        yield from super().nodes_()
        for partition_term in self._partition:
            if isinstance(partition_term, Node):
                yield from partition_term.nodes_()
        for ordering_term, __ in self._orderbys:
            if isinstance(ordering_term, Node):
                yield from ordering_term.nodes_()

    @BuilderMethods.builder
    def over(self, *terms: Any) -> Self:
        self._include_over = True
        self._partition += terms
        return self

    @BuilderMethods.builder
    def orderby(self, *terms: Any, **kwargs: Any) -> Self:
        self._include_over = True
        self._orderbys += [(term, kwargs.get("order")) for term in terms]
        return self

    def _orderby_field(self, field: Field, orient: Order | None, sql_context: SqlContext) -> str:
        return sql_context.dialect.renderers.get_window_ordering_clause_sql(
            self, field, field.get_sql(sql_context), orient, sql_context
        )

    def get_partition_sql(self, sql_context: SqlContext) -> str:
        """The window of the call - ``PARTITION BY ... ORDER BY ...`` - without the parentheses.

        Args:
            sql_context: How the SQL is written.

        Returns:
            The SQL.
        """
        terms = []
        if self._partition:
            terms.append(
                "PARTITION BY {args}".format(
                    args=",".join(
                        partition_term.get_sql(sql_context)
                        if hasattr(partition_term, "get_sql")
                        else str(partition_term)
                        for partition_term in self._partition
                    )
                )
            )

        if self._orderbys:
            terms.append(
                "ORDER BY {orderby}".format(
                    orderby=",".join(
                        self._orderby_field(field, orient, sql_context) for field, orient in self._orderbys
                    )
                )
            )

        return " ".join(terms)

    def get_function_sql(self, sql_context: SqlContext) -> str:
        function_sql = super().get_function_sql(sql_context)
        partition_sql = self.get_partition_sql(sql_context)

        sql = function_sql
        if self._include_over:
            sql += f" OVER({partition_sql})"

        return sql
