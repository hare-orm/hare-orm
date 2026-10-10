from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING, Any

from hare.sql.builder_methods import BuilderMethods
from hare.sql.enums import Order
from hare.sql.sql_context import SqlContext
from hare.sql.terms.criteria.criterion import Criterion
from hare.sql.terms.node import TNode
from hare.sql.terms.term import Term

if TYPE_CHECKING:
    from typing import Self

    from hare.sql.builder.tables.table import Table
from hare.sql.terms.functions.function import Function


class AggregateFunction(Function):
    is_aggregate = True

    def __init__(self, name: str, *args: Any, **kwargs: Any) -> None:
        super().__init__(name, *args, **kwargs)

        self._filters: list[Any] = []
        self._include_filter = False
        self._argument_orderings: list[tuple[Term, Order | None]] = []

    def __copy__(self) -> Self:
        # Same rationale as Case.__copy__ above: filter() mutates _filters in place via +=,
        # so the shallow Term.__copy__ would otherwise share one list across every branch
        # built from the same base function.
        new_term = super().__copy__()
        new_term._filters = list(self._filters)
        new_term._argument_orderings = list(self._argument_orderings)
        return new_term

    def nodes_(self) -> Iterator[TNode]:
        yield from super().nodes_()
        for aggregate_filter in self._filters:
            yield from aggregate_filter.nodes_()
        for ordering_term, __ in self._argument_orderings:
            yield from ordering_term.nodes_()

    @BuilderMethods.builder
    def order_arguments(self, *orderings: tuple[Term, Order | None]) -> Self:
        """Orders the rows the aggregate reads - ``ARRAY_AGG(x ORDER BY y DESC)``.

        Args:
            orderings: Each term with its direction.
        """
        self._argument_orderings += orderings
        return self

    def get_special_parameters_sql(self, sql_context: SqlContext) -> Any:
        if not self._argument_orderings:
            return super().get_special_parameters_sql(sql_context)
        ordering_sql = ",".join(
            self.get_arg_sql(term, sql_context) if order is None else f"{self.get_arg_sql(term, sql_context)} {order}"
            for term, order in self._argument_orderings
        )
        return f"ORDER BY {ordering_sql}"

    @BuilderMethods.builder
    def replace_table(self, current_table: Table | None, new_table: Table | None) -> Self:
        """Replaces all occurrences of the specified table with the new table, in the arguments
        and the ``FILTER`` condition.

        Args:
            current_table: The table to be replaced.
            new_table: The table to replace with.

        Returns:
            A copy of the function with the tables replaced.
        """
        self.args = [parameter.replace_table(current_table, new_table) for parameter in self.args]
        self._filters = [
            aggregate_filter.replace_table(current_table, new_table) for aggregate_filter in self._filters
        ]
        self._argument_orderings = [
            (ordering_term.replace_table(current_table, new_table), order)
            for ordering_term, order in self._argument_orderings
        ]
        return self

    @BuilderMethods.builder
    def filter(self, *filters: Any) -> Self:
        self._include_filter = True
        self._filters += filters
        return self

    def get_filter_sql(self, sql_context: SqlContext) -> str:
        """The condition of the aggregate's FILTER clause.

        Args:
            sql_context: The context the aggregate renders in.

        Returns:
            ``WHERE <condition>``, empty without a filter.
        """
        if not self._include_filter:
            return ""
        return f"WHERE {Criterion.all(self._filters).get_sql(sql_context)}"

    def get_function_sql(self, sql_context: SqlContext) -> str:
        sql = super().get_function_sql(sql_context)
        filter_sql = self.get_filter_sql(sql_context)

        if self._include_filter:
            sql += f" FILTER({filter_sql})"

        return sql
