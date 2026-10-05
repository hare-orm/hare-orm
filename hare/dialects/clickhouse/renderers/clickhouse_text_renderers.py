from __future__ import annotations

from typing import TYPE_CHECKING

from hare.dialects.clickhouse.constants import CLICKHOUSE_DATETIME_COLUMN_TYPE
from hare.dialects.clickhouse.renderers.clickhouse_temporal_renderers import ClickhouseTemporalRenderers
from hare.dialects.clickhouse.renderers.constants import (
    CLICKHOUSE_DIGEST_FUNCTIONS,
    CLICKHOUSE_GENERIC_FUNCTION_NAMES,
    CLICKHOUSE_TEXT_FUNCTION_NAMES,
)
from hare.sql import functions
from hare.sql.enums import CastType
from hare.sql.sql_context import SqlContext

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.renderers.term_renderers import TermRenderers
    from hare.sql.terms.functions.function import Function


class ClickhouseTextRenderers:
    """How ClickHouse writes text: the text functions counting characters, the digests as hex,
    concatenation, casts, and a boolean written as text."""

    @classmethod
    def register(cls, renderers: TermRenderers) -> None:
        """Registers the text renderers on ClickHouse's renderers.

        Args:
            renderers: The renderers.
        """
        renderers.register(functions.TextFunction, cls.render_text_function)
        renderers.register(functions.CastTo, cls.render_cast_to)
        renderers.register(functions.BooleanAsText, cls.render_boolean_as_text)
        for function_name in CLICKHOUSE_GENERIC_FUNCTION_NAMES:
            renderers.register_function(function_name, cls.render_generic_function)

    @staticmethod
    def render_generic_function(function: Function, sql_context: SqlContext) -> str:
        """A plain SQL function under ClickHouse's own name."""
        args_sql = ",".join(function.get_arg_sql(argument, sql_context) for argument in function.args)
        return f"{CLICKHOUSE_GENERIC_FUNCTION_NAMES[function.name]}({args_sql})"

    @staticmethod
    def render_text_function(function: functions.TextFunction, sql_context: SqlContext) -> str:
        """The function with its first argument read as text."""
        args_sql = [function.get_arg_sql(arg, sql_context) for arg in function.args]
        if function.name in CLICKHOUSE_DIGEST_FUNCTIONS:
            return f"lower(hex({function.name}(toString({args_sql[0]}))))"
        if function.name != "CHR":
            args_sql[0] = f"toString({args_sql[0]})"
        name = CLICKHOUSE_TEXT_FUNCTION_NAMES.get(function.name, function.name)
        return f"{name}({','.join(args_sql)})"

    @staticmethod
    def render_cast_to(function: functions.CastTo, sql_context: SqlContext) -> str:
        """The cast - to the column type of the target field, a time of day as its text."""
        term_sql = function.get_arg_sql(function.args[0], sql_context)
        if function.target == CastType.TIME:
            return ClickhouseTemporalRenderers.get_time_text_sql(term_sql, "'UTC'")
        if function.target == CastType.DATETIME:
            return f"CAST({term_sql} AS {CLICKHOUSE_DATETIME_COLUMN_TYPE})"
        if function.target == CastType.TEXT:
            return f"toString({term_sql})"
        return f"CAST({term_sql} AS {function.target_field.get_column_type(sql_context.dialect)})"

    @staticmethod
    def render_boolean_as_text(function: functions.BooleanAsText, sql_context: SqlContext) -> str:
        """``'true'``/``'false'`` - a comparison is a number in ClickHouse, which its own text would show."""
        term_sql = function.get_arg_sql(function.args[0], sql_context)
        return f"(CASE WHEN {term_sql} IS NULL THEN NULL WHEN {term_sql} THEN 'true' ELSE 'false' END)"
