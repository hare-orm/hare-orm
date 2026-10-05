from __future__ import annotations

from hare.dialects.clickhouse.lookups.constants import (
    CLICKHOUSE_JSON_VALUE_TEXT_FUNCTION_NAME,
    CLICKHOUSE_JSON_VALUE_TEXT_SQL,
)
from hare.sql.sql_context import SqlContext
from hare.sql.terms.functions.function import Function
from hare.sql.terms.term import Term


class ClickhouseJsonValueText(Function):
    """A JSON text re-written as ClickHouse writes the value at a path of a stored document - the
    value a filter compares with that path."""

    def __init__(self, json_text: Term) -> None:
        """
        Args:
            json_text: The value's JSON text.
        """
        super().__init__(CLICKHOUSE_JSON_VALUE_TEXT_FUNCTION_NAME, json_text)

    def get_function_sql(self, sql_context: SqlContext) -> str:
        return CLICKHOUSE_JSON_VALUE_TEXT_SQL.format(value=self.get_arg_sql(self.args[0], sql_context))
