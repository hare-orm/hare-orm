from __future__ import annotations

from hare.dialects.clickhouse.lookups.constants import (
    CLICKHOUSE_ARRAY_ELEMENTS_KEY_FUNCTION_NAME,
    CLICKHOUSE_ARRAY_ELEMENTS_KEY_SQL,
)
from hare.sql.sql_context import SqlContext
from hare.sql.terms.functions.function import Function
from hare.sql.terms.term import Term
from hare.sql.terms.values.literal_value import LiteralValue


class ClickhouseArrayElementsKey(Function):
    """The key an array orders by - the array of the key of each element, as PostgreSQL compares an
    array element by element."""

    def __init__(self, array: Term, variable: LiteralValue, element_key: Term) -> None:
        """
        Args:
            array: The array.
            variable: The lambda variable an element is named by in ``element_key``.
            element_key: The key of one element.
        """
        super().__init__(CLICKHOUSE_ARRAY_ELEMENTS_KEY_FUNCTION_NAME, array, element_key)
        self.variable = variable

    def get_function_sql(self, sql_context: SqlContext) -> str:
        return CLICKHOUSE_ARRAY_ELEMENTS_KEY_SQL.format(
            variable=self.variable.get_sql(sql_context),
            element_key=self.get_arg_sql(self.args[1], sql_context),
            value=self.get_arg_sql(self.args[0], sql_context),
        )
