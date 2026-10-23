from __future__ import annotations

from copy import copy
from typing import TYPE_CHECKING, cast

from hare.dialects.clickhouse.renderers.constants import (
    CLICKHOUSE_ARRAY_AGGREGATE_NAME,
    CLICKHOUSE_DECIMAL_AVERAGE_NAME,
    CLICKHOUSE_GROUP_ARRAY_FUNCTION_NAME,
    CLICKHOUSE_GROUP_DISTINCT_ARRAY_FUNCTION_NAME,
    CLICKHOUSE_GROUPED_AGGREGATE_NAMES,
    CLICKHOUSE_NULL_FOR_NO_ROWS_AGGREGATE_NAMES,
    CLICKHOUSE_SAMPLE_AGGREGATE_NAMES,
    CLICKHOUSE_VALUE_COUNT_FUNCTION_NAME,
)
from hare.exceptions import UnSupportedError
from hare.sql.constants import DECIMAL_QUOTIENT_SCALE
from hare.sql.enums import Order
from hare.sql.terms.functions.aggregate_function import AggregateFunction
from hare.sql.terms.functions.function import Function

if TYPE_CHECKING:
    from hare.dialects.base.renderers.term_renderers import TermRenderers
    from hare.sql.functions.distinct_option_function import DistinctOptionFunction
    from hare.sql.sql_context import SqlContext


class ClickhouseAggregateRenderers:
    """How ClickHouse writes the aggregates whose result over no rows would be a value no row holds - by
    their ``-OrNull`` form, as ClickHouse otherwise returns the column type's default or NaN there; a sum
    over no rows is ClickHouse's own 0. An aggregate of
    a grouped statement reads at least one row and keeps its plain, cheaper form; so does one of a
    type ClickHouse holds no NULL of (an array, a map, a tuple), giving its empty value. A sample deviation
    is NULL over fewer than two values, as in SQL - ClickHouse gives NaN over one. ``ARRAY_AGG`` is
    ``groupArray``, keeping NULL values."""

    @classmethod
    def register(cls, renderers: TermRenderers) -> None:
        """Registers the aggregate renderers on ClickHouse's renderers.

        Args:
            renderers: The renderers.
        """
        for function_name in CLICKHOUSE_NULL_FOR_NO_ROWS_AGGREGATE_NAMES:
            renderers.register_function(function_name, cls.render_null_for_no_rows_aggregate)
        renderers.register_function(CLICKHOUSE_ARRAY_AGGREGATE_NAME, cls.render_array_aggregate)
        renderers.register_function(CLICKHOUSE_DECIMAL_AVERAGE_NAME, cls.render_decimal_average)

    @classmethod
    def render_decimal_average(cls, function: Function, sql_context: SqlContext) -> str:
        """An average of Decimals as their exact sum divided by their count at the quotient scale -
        ClickHouse's ``avg()`` of a Decimal is a Float64. Its ``DISTINCT``, ``FILTER`` and window are
        those of both."""
        total = copy(function)
        total.name = "SUM"
        count = copy(function)
        count.name = CLICKHOUSE_VALUE_COUNT_FUNCTION_NAME
        count_sql = count.get_function_sql(sql_context)
        total_sql = total.get_function_sql(sql_context)
        return f"if({count_sql} = 0, NULL, toDecimal256({total_sql}, {DECIMAL_QUOTIENT_SCALE}) / {count_sql})"

    @staticmethod
    def render_array_aggregate(function: Function, sql_context: SqlContext) -> str:
        """``ARRAY_AGG`` as ``groupArray`` - of a tuple holding each value, as ``groupArray`` leaves a
        NULL value out; its ``DISTINCT`` as ``groupUniqArray``, its ``FILTER`` kept, its ``ORDER BY``
        as a sort of the collected rows by the ordering values collected with them, a NULL one where
        hare's ordering puts it (last going up, first going down).

        Raises:
            UnSupportedError: The orderings go both up and down - ClickHouse sorts an array one way.
        """
        aggregate = cast("DistinctOptionFunction", function)
        orderings = aggregate._argument_orderings
        directions = {order is None or order.is_ascending for _, order in orderings}
        if len(directions) > 1:
            raise UnSupportedError(
                "ArrayAgg(order_by=...) orders by ascending or by descending values on ClickHouse, not by both"
            )
        collected = copy(aggregate)
        collected.name = (
            CLICKHOUSE_GROUP_DISTINCT_ARRAY_FUNCTION_NAME
            if aggregate._distinct
            else CLICKHOUSE_GROUP_ARRAY_FUNCTION_NAME
        )
        collected._distinct = False
        collected._argument_orderings = []
        collected.args = [Function("tuple", aggregate.args[0], *(term for term, _ in orderings))]
        collected_sql = collected.get_function_sql(sql_context)
        if orderings:
            is_ascending = directions == {True}
            keys = []
            for position, (_, order) in enumerate(orderings):
                value_sql = f"hare_collected.{position + 2}"
                nulls_first = (order or Order.ASC).puts_nulls_first(sorts_nulls_first=False)
                # A NULL value sorts by a flag put before it, 0 on the side the NULLs go to.
                null_flag_function = "isNotNull" if nulls_first == is_ascending else "isNull"
                keys.append(f"{null_flag_function}({value_sql}),{value_sql}")
            sort_function = "arraySort" if is_ascending else "arrayReverseSort"
            collected_sql = f"{sort_function}(hare_collected -> tuple({','.join(keys)}), {collected_sql})"
        return f"arrayMap(hare_collected -> hare_collected.1, {collected_sql})"

    @staticmethod
    def render_null_for_no_rows_aggregate(function: Function, sql_context: SqlContext) -> str:
        """The aggregate under its ``-OrNull`` name - its ``DISTINCT`` and ``FILTER`` kept. Under
        its own name in a grouped statement, unless a ``FILTER`` or a window frame can leave it no
        row, and of a type holding no NULL."""
        if (isinstance(function, AggregateFunction) and not function.result_holds_null) or (
            getattr(sql_context, "rows_are_grouped", False)
            and not function.is_analytic
            and not getattr(function, "_include_filter", False)
        ):
            grouped_function = copy(function)
            grouped_function.name = CLICKHOUSE_GROUPED_AGGREGATE_NAMES.get(function.name, function.name)
            function_sql = grouped_function.get_function_sql(sql_context)
        else:
            renamed_function = copy(function)
            renamed_function.name = CLICKHOUSE_NULL_FOR_NO_ROWS_AGGREGATE_NAMES[function.name]
            function_sql = renamed_function.get_function_sql(sql_context)
        if function.name not in CLICKHOUSE_SAMPLE_AGGREGATE_NAMES:
            return function_sql
        # The values it reads counted with its own DISTINCT, FILTER and window.
        count_function = copy(function)
        count_function.name = CLICKHOUSE_VALUE_COUNT_FUNCTION_NAME
        return f"if({count_function.get_function_sql(sql_context)} > 1, {function_sql}, NULL)"
