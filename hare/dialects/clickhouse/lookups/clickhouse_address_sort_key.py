from __future__ import annotations

from hare.dialects.clickhouse.lookups.constants import (
    CLICKHOUSE_ADDRESS_SORT_KEY_FUNCTION_NAME,
    CLICKHOUSE_ADDRESS_SORT_KEY_SQL,
)
from hare.sql.sql_context import SqlContext
from hare.sql.terms.functions.function import Function
from hare.sql.terms.term import Term


class ClickhouseAddressSortKey(Function):
    """The key an ``IPv6`` value compares and orders by, as PostgreSQL orders ``inet``: an IPv4 address
    (held as ``::ffff:a.b.c.d``) before every IPv6 one, then by the address's bytes."""

    def __init__(self, address: Term) -> None:
        """
        Args:
            address: The ``IPv6`` value.
        """
        super().__init__(CLICKHOUSE_ADDRESS_SORT_KEY_FUNCTION_NAME, address)

    def get_function_sql(self, sql_context: SqlContext) -> str:
        return CLICKHOUSE_ADDRESS_SORT_KEY_SQL.format(value=self.get_arg_sql(self.args[0], sql_context))
