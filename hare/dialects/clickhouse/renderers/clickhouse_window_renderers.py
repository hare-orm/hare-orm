from __future__ import annotations

from copy import copy
from typing import TYPE_CHECKING, cast

from hare.dialects.clickhouse.renderers.constants import (
    CLICKHOUSE_OFFSET_FUNCTION_NAMES,
    CLICKHOUSE_UP_TO_PEERS_FRAME,
    CLICKHOUSE_WHOLE_PARTITION_FRAME,
)
from hare.sql.enums import AnalyticFunctionName
from hare.sql.terms.functions.analytic_function import AnalyticFunction
from hare.sql.terms.functions.function import Function
from hare.sql.terms.star import Star

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.renderers.term_renderers import TermRenderers
    from hare.sql.sql_context import SqlContext


class ClickhouseWindowRenderers:
    """How ClickHouse writes the window functions it names otherwise or lacks: ``LAG``/``LEAD`` read
    the whole partition (``lagInFrame``/``leadInFrame`` read the frame and give the type's default
    past its edge), ``CUME_DIST`` is the share of the partition's rows up to the current one's peers,
    ``COUNT`` of a column counts it through an expression - ClickHouse 25.8 counts a Nullable
    column in a window through a subcolumn it then can't find - and ``FIRST_VALUE``/``LAST_VALUE``
    read a NULL as a value."""

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
        for function_name in (AnalyticFunctionName.FIRST_VALUE, AnalyticFunctionName.LAST_VALUE):
            renderers.register_function(function_name, cls.render_value_respecting_nulls)

    @staticmethod
    def render_value_respecting_nulls(function: Function, sql_context: SqlContext) -> str:
        """``FIRST_VALUE``/``LAST_VALUE`` of the frame's first or last row whatever it holds - ClickHouse
        skips a NULL there by default."""
        if not isinstance(function, AnalyticFunction):
            return function.get_function_sql(sql_context)
        arguments_sql = ", ".join(function.get_arg_sql(argument, sql_context) for argument in function.args)
        return f"{function.name}({arguments_sql}) RESPECT NULLS OVER({function.get_partition_sql(sql_context)})"

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
    def render_count(function: Function, sql_context: SqlContext) -> str:
        """``COUNT`` - of a column in a window through ``identity()``."""
        if not isinstance(function, AnalyticFunction) or isinstance(function.args[0], Star):
            return function.get_function_sql(sql_context)
        counted = copy(function)
        counted.args = [Function("identity", function.args[0])]
        return counted.get_function_sql(sql_context)
