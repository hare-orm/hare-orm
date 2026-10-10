from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING, Any

from hare.dialects.clickhouse.client.declarations import ClickhouseValueSet
from hare.sql.terms.parameters.list_parameter import ListParameter
from hare.sql.terms.term import Term
from hare.sql.terms.values.value_wrapper import ValueWrapper

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.sql import SqlContext
    from hare.sql.terms.node import TNode


class ClickhouseValueSetValues(Term, ListParameter):
    """The values of a long ``__in`` list bound as one ``ClickhouseValueSet`` parameter - an external table
    of a read, a list of literals elsewhere.

    Args:
        value_set: The values.
    """

    def __init__(self, value_set: ClickhouseValueSet) -> None:
        super().__init__()
        self.value_set = ValueWrapper(value_set)

    def nodes_(self) -> Iterator[TNode]:
        yield self  # type:ignore[misc]
        yield from self.value_set.nodes_()

    def get_sql(self, sql_context: SqlContext) -> str:
        return self.value_set.get_sql(sql_context)

    def get_parameter_source(self) -> Term:
        return self.value_set

    def get_parameter(self, values: list[Any], *, holds_no_term: bool = False) -> ClickhouseValueSet | None:
        # Local import: the large list class names this one.
        from hare.dialects.clickhouse.lookups.in_list.clickhouse_large_in_list import ClickhouseLargeInList

        current = self.value_set.value
        if len(current.column_types) == 1:
            return ClickhouseLargeInList.get_value_set(values)
        return ClickhouseLargeInList.get_row_value_set(values)
