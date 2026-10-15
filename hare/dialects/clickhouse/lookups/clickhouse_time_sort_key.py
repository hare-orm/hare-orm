from __future__ import annotations

from hare.dialects.clickhouse.lookups.constants import (
    CLICKHOUSE_TIME_OFFSET_SECONDS_SQL,
    CLICKHOUSE_TIME_OFFSET_TEXT_SQL,
    CLICKHOUSE_TIME_SORT_KEY_FUNCTION_NAME,
    CLICKHOUSE_TIME_SORT_KEY_SQL,
    CLICKHOUSE_TIME_WALL_CLOCK_SQL,
)
from hare.sql.sql_context import SqlContext
from hare.sql.terms.functions.function import Function
from hare.sql.terms.term import Term


class ClickhouseTimeSortKey(Function):
    """The key the text of a time of day compares and orders by, as PostgreSQL orders ``TIMETZ`` - by
    the UTC time, then by the offset - not the text itself, which puts ``10:00+03:00`` after
    ``09:00+00:00``."""

    def __init__(self, time_text: Term) -> None:
        """
        Args:
            time_text: The time's ISO text.
        """
        super().__init__(CLICKHOUSE_TIME_SORT_KEY_FUNCTION_NAME, time_text)

    def get_function_sql(self, sql_context: SqlContext) -> str:
        value = self.get_arg_sql(self.args[0], sql_context)
        offset_seconds = CLICKHOUSE_TIME_OFFSET_SECONDS_SQL.format(
            offset=CLICKHOUSE_TIME_OFFSET_TEXT_SQL.format(value=value)
        )
        return CLICKHOUSE_TIME_SORT_KEY_SQL.format(
            wall_clock=CLICKHOUSE_TIME_WALL_CLOCK_SQL.format(value=value), offset_seconds=offset_seconds
        )
