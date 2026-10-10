from __future__ import annotations

from copy import copy
from typing import TYPE_CHECKING, cast

from hare.dialects.clickhouse.renderers.constants import (
    CLICKHOUSE_ARRAY_AGGREGATE_NAME,
    CLICKHOUSE_GROUP_ARRAY_FUNCTION_NAME,
    CLICKHOUSE_GROUP_DISTINCT_ARRAY_FUNCTION_NAME,
    CLICKHOUSE_NULL_FOR_NO_ROWS_AGGREGATE_NAMES,
    CLICKHOUSE_SAMPLE_AGGREGATE_NAMES,
    CLICKHOUSE_VALUE_COUNT_FUNCTION_NAME,
)
from hare.exceptions import UnSupportedError
from hare.sql.enums import Order
from hare.sql.terms.functions.function import Function

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.renderers.term_renderers import TermRenderers
    from hare.sql.functions.distinct_option_function import DistinctOptionFunction
    from hare.sql.sql_context import SqlContext


class ClickhouseAggregateRenderers:
    """How ClickHouse writes the aggregates whose result over no rows is NULL in SQL - by their
    ``-OrNull`` form, as ClickHouse otherwise returns the column type's default there. An aggregate of
    a grouped statement reads at least one row and keeps its plain, cheaper form. A sample deviation
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

    @staticmethod
    def render_array_aggregate(function: Function, sql_context: SqlContext) -> str:
        """``ARRAY_AGG`` as ``groupArray`` - of a tuple holding each value, as ``groupArray`` leaves a
        NULL value out; its ``DISTINCT`` as ``groupUniqArray``, its ``FILTER`` kept, its ``ORDER BY``
        as a sort of the collected rows by the ordering values collected with them.

        Raises:
            UnSupportedError: The orderings go both up and down - ClickHouse sorts an array one way.
        """
        aggregate = cast("DistinctOptionFunction", function)
        orderings = aggregate._argument_orderings
        directions = {Order.DESC if order == Order.DESC else Order.ASC for _, order in orderings}
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
            sort_function = "arrayReverseSort" if directions == {Order.DESC} else "arraySort"
            keys_sql = ",".join(f"hare_collected.{position + 2}" for position in range(len(orderings)))
            collected_sql = f"{sort_function}(hare_collected -> tuple({keys_sql}), {collected_sql})"
        return f"arrayMap(hare_collected -> hare_collected.1, {collected_sql})"

    @staticmethod
    def render_null_for_no_rows_aggregate(function: Function, sql_context: SqlContext) -> str:
        """The aggregate under its ``-OrNull`` name - its ``DISTINCT`` and ``FILTER`` kept. Under
        its own name in a grouped statement, unless a ``FILTER`` or a window frame can leave it no
        row."""
        if (
            getattr(sql_context, "rows_are_grouped", False)
            and not function.is_analytic
            and not getattr(function, "_include_filter", False)
        ):
            function_sql = function.get_function_sql(sql_context)
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
