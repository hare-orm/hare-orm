from __future__ import annotations

from hare.dialects.clickhouse.lookups.constants import (
    CLICKHOUSE_JSON_SORT_KEY_FUNCTION_NAME,
    CLICKHOUSE_JSON_SORT_KEY_SQL,
)
from hare.sql.sql_context import SqlContext
from hare.sql.terms.functions.function import Function
from hare.sql.terms.term import Term


class ClickhouseJsonSortKey(Function):
    """The key a JSON value's text compares and orders by, as PostgreSQL orders ``jsonb`` - not the
    text itself, which puts ``10`` before ``9``."""

    def __init__(self, json_text: Term) -> None:
        """
        Args:
            json_text: The value's JSON text.
        """
        super().__init__(CLICKHOUSE_JSON_SORT_KEY_FUNCTION_NAME, json_text)

    def get_function_sql(self, sql_context: SqlContext) -> str:
        return CLICKHOUSE_JSON_SORT_KEY_SQL.format(value=self.get_arg_sql(self.args[0], sql_context))
