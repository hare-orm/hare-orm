from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import TYPE_CHECKING, Any

from hare.dialects.clickhouse.client.declarations import ClickhouseValueSet
from hare.sql.terms.parameters.list_parameter import ListParameter
from hare.sql.terms.term import Term
from hare.sql.terms.values.value_wrapper import ValueWrapper

if TYPE_CHECKING:
    from hare.sql import SqlContext
    from hare.sql.terms.node import Node


class ClickhouseValueSetValues(Term, ListParameter):
    """The values of a long ``__in`` list bound as one ``ClickhouseValueSet`` parameter - an external table
    of a read, a list of literals elsewhere.

    Args:
        value_set: The values.
    """

    def __init__(self, value_set: ClickhouseValueSet) -> None:
        super().__init__()
        self.value_set = ValueWrapper(value_set)

    def get_children(self) -> Iterator[Node]:
        yield self.value_set

    def get_sql(self, sql_context: SqlContext) -> str:
        return self.value_set.get_sql(sql_context)

    def get_parameter_source(self) -> Term:
        return self.value_set

    def get_parameter_encoder(self) -> Callable[[list[Any]], ClickhouseValueSet | None]:
        from hare.dialects.clickhouse.lookups.in_list.clickhouse_large_in_list import ClickhouseLargeInList

        return (
            ClickhouseLargeInList().get_value_set
            if len(self.value_set.value.column_types) == 1
            else ClickhouseLargeInList().get_row_value_set
        )
