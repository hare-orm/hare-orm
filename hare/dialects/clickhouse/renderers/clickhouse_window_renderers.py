from __future__ import annotations

from copy import copy
from typing import TYPE_CHECKING, cast

from hare.dialects.clickhouse.renderers.constants import (
    CLICKHOUSE_OFFSET_FUNCTION_NAMES,
    CLICKHOUSE_SIGNED_RESULT_TEMPLATE,
    CLICKHOUSE_UNSIGNED_RESULT_WINDOW_FUNCTION_NAMES,
    CLICKHOUSE_UP_TO_PEERS_FRAME,
    CLICKHOUSE_WHOLE_PARTITION_FRAME,
)
from hare.sql.enums import AnalyticFunctionName
from hare.sql.terms.functions.analytic_function import AnalyticFunction
from hare.sql.terms.functions.function import Function
from hare.sql.terms.star import Star

if TYPE_CHECKING:
    from hare.dialects.base.renderers.term_renderers import TermRenderers
    from hare.sql.sql_context import SqlContext


class ClickhouseWindowRenderers:
    """How ClickHouse writes the window functions it names otherwise or lacks: ``LAG``/``LEAD`` read
    the whole partition (``lagInFrame``/``leadInFrame`` read the frame and give the type's default
    past its edge), ``CUME_DIST`` is the share of the partition's rows up to the current one's peers,
    ``NTH_VALUE`` past the partition's rows NULL. A row number, a rank, a tile and a count are
    signed."""

    @classmethod
    def register(cls, renderers: TermRenderers) -> None:
        """Registers the window function renderers on ClickHouse's renderers.

        Args:
            renderers: The renderers.
        """
        for function_name in CLICKHOUSE_OFFSET_FUNCTION_NAMES:
            renderers.register_function(function_name, cls.render_offset_function)
        renderers.register_function(AnalyticFunctionName.CUME_DIST, cls.render_cumulative_distribution)
        renderers.register_function(AnalyticFunctionName.COUNT, cls.render_count)
        renderers.register_function(AnalyticFunctionName.NTH_VALUE, cls.render_nth_value)
        for function_name in CLICKHOUSE_UNSIGNED_RESULT_WINDOW_FUNCTION_NAMES:
            renderers.register_function(function_name, cls.render_signed_result)

    @staticmethod
    def render_offset_function(function: Function, sql_context: SqlContext) -> str:
        """The value of the row ``offset`` rows away in the partition, the default (NULL without one)
        past its edge - cast to the value's type, which ClickHouse requires of it."""
        window_function = cast("AnalyticFunction", function)
        value_sql, offset_sql, default_sql = (window_function.get_arg_sql(arg, sql_context) for arg in function.args)
        nullable_value_sql = f"toNullable({value_sql})"
        return (
            f"{CLICKHOUSE_OFFSET_FUNCTION_NAMES[function.name]}({nullable_value_sql}, {offset_sql}, "
            f"CAST({default_sql}, toTypeName({nullable_value_sql}))) "
            f"OVER({window_function.get_partition_sql(sql_context)} {CLICKHOUSE_WHOLE_PARTITION_FRAME})"
        )

    @staticmethod
    def render_cumulative_distribution(function: Function, sql_context: SqlContext) -> str:
        """The share of the partition's rows ordered up to the current one's peers."""
        partition_sql = cast("AnalyticFunction", function).get_partition_sql(sql_context)
        return (
            f"(toFloat64(count() OVER({partition_sql} {CLICKHOUSE_UP_TO_PEERS_FRAME})) "
            f"/ count() OVER({partition_sql} {CLICKHOUSE_WHOLE_PARTITION_FRAME}))"
        )

    @staticmethod
    def render_signed_result(function: Function, sql_context: SqlContext) -> str:
        """A window function's UInt64 as a signed integer - ClickHouse finds no common type of a UInt64
        and a signed integer in ``CASE``, ``COALESCE`` or ``greatest()``."""
        return CLICKHOUSE_SIGNED_RESULT_TEMPLATE.format(function.get_function_sql(sql_context))

    @staticmethod
    def render_count(function: Function, sql_context: SqlContext) -> str:
        """``COUNT`` as a signed integer - a window's of a column through ``identity()``: the server
        rewrites the count of a nullable column to read its null map, which it then doesn't find in a
        window (``NOT_FOUND_COLUMN_IN_BLOCK``)."""
        if function.is_analytic and function.args and not isinstance(function.args[0], Star):
            function = copy(function)
            function.args = [Function("identity", function.args[0]), *function.args[1:]]
        return CLICKHOUSE_SIGNED_RESULT_TEMPLATE.format(function.get_function_sql(sql_context))

    @staticmethod
    def render_nth_value(function: Function, sql_context: SqlContext) -> str:
        """``NTH_VALUE`` of the value made nullable - past the partition's rows ClickHouse gives the
        value type's default (0), not NULL."""
        nullable_function = copy(function)
        nullable_function.args = [Function("toNullable", function.args[0]), *function.args[1:]]
        return nullable_function.get_function_sql(sql_context)
