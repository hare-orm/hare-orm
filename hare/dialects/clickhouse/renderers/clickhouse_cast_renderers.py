from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

from hare.dialects.clickhouse.renderers.constants import (
    CLICKHOUSE_CAST_FUNCTIONS,
    CLICKHOUSE_NUMERIC_CAST_TYPE,
    CLICKHOUSE_NUMERIC_TYPE_NAME,
)
from hare.sql import functions
from hare.sql.sql_context import SqlContext
from hare.sql.terms.values.value_wrapper import ValueWrapper
from hare.sql.types.sql_type import SqlType

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.renderers.term_renderers import TermRenderers


class ClickhouseCastRenderers:
    """How ClickHouse writes a cast to a plain SQL type: ClickHouse's ``NUMERIC`` is a decimal with no
    digits after the point and its ``FLOAT`` a 32-bit float, and its ``CAST`` of a NULL to a type that
    isn't ``Nullable`` fails - the conversion functions keep the NULL."""

    @classmethod
    def register(cls, renderers: TermRenderers) -> None:
        """Registers the cast renderer on ClickHouse's renderers.

        Args:
            renderers: The renderers.
        """
        renderers.register(functions.Cast, cls.render_cast)

    @staticmethod
    def render_cast(function: functions.Cast, sql_context: SqlContext) -> str:
        """The conversion function of the type, a decimal literal as it is - it is written as a
        decimal of its own scale."""
        as_type = function.as_type
        type_name = as_type.name if type(as_type) is SqlType else str(as_type).upper()
        argument = function.args[0]
        argument_sql = function.get_arg_sql(argument, sql_context)
        if type_name == CLICKHOUSE_NUMERIC_TYPE_NAME:
            if isinstance(argument, ValueWrapper) and isinstance(argument.value, Decimal):
                return argument_sql
            return CLICKHOUSE_NUMERIC_CAST_TYPE.format(argument_sql)
        conversion_function = CLICKHOUSE_CAST_FUNCTIONS.get(type_name)
        if conversion_function is None:
            return function.get_function_sql(sql_context)
        return f"{conversion_function}({argument_sql})"
