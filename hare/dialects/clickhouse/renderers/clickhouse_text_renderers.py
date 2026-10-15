from __future__ import annotations

from typing import TYPE_CHECKING

from hare.dialects.clickhouse.constants import CLICKHOUSE_DATETIME_COLUMN_TYPE
from hare.dialects.clickhouse.lookups.constants import CLICKHOUSE_ADDRESS_TEXT_SQL
from hare.dialects.clickhouse.renderers.clickhouse_temporal_renderers import ClickhouseTemporalRenderers
from hare.dialects.clickhouse.renderers.clickhouse_value_casts import ClickhouseValueCasts
from hare.dialects.clickhouse.renderers.clickhouse_value_texts import ClickhouseValueTexts
from hare.dialects.clickhouse.renderers.constants import (
    CLICKHOUSE_CHARACTER_TEMPLATE,
    CLICKHOUSE_CODE_POINT_TEMPLATE,
    CLICKHOUSE_DIGEST_FUNCTIONS,
    CLICKHOUSE_GENERIC_FUNCTION_NAMES,
    CLICKHOUSE_LOWER_TEMPLATE,
    CLICKHOUSE_SUBSTRING_TEMPLATE,
    CLICKHOUSE_TEXT_FUNCTION_NAMES,
    CLICKHOUSE_UPPER_TEMPLATE,
)
from hare.sql import functions
from hare.sql.enums import CastType
from hare.sql.functions.address_text import AddressText
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
        renderers.register(AddressText, cls.render_address_text)
        for function_name in CLICKHOUSE_GENERIC_FUNCTION_NAMES:
            renderers.register_function(function_name, cls.render_generic_function)
        renderers.register_function("UPPER", cls.render_upper)
        renderers.register_function("LOWER", cls.render_lower)

    @staticmethod
    def render_generic_function(function: Function, sql_context: SqlContext) -> str:
        """A plain SQL function under ClickHouse's own name."""
        args_sql = ",".join(function.get_arg_sql(argument, sql_context) for argument in function.args)
        return f"{CLICKHOUSE_GENERIC_FUNCTION_NAMES[function.name]}({args_sql})"

    @staticmethod
    def render_upper(function: Function, sql_context: SqlContext) -> str:
        """The text in uppercase character by character, like PostgreSQL."""
        return CLICKHOUSE_UPPER_TEMPLATE.format(function.get_arg_sql(function.args[0], sql_context))

    @staticmethod
    def render_lower(function: Function, sql_context: SqlContext) -> str:
        """The text in lowercase character by character, like PostgreSQL."""
        return CLICKHOUSE_LOWER_TEMPLATE.format(function.get_arg_sql(function.args[0], sql_context))

    @staticmethod
    def render_text_function(function: functions.TextFunction, sql_context: SqlContext) -> str:
        """The function with its first argument read as text."""
        args_sql = [function.get_arg_sql(arg, sql_context) for arg in function.args]
        if function.name in CLICKHOUSE_DIGEST_FUNCTIONS:
            return f"lower(hex({function.name}(toString({args_sql[0]}))))"
        if function.name == "CHR":
            return CLICKHOUSE_CHARACTER_TEMPLATE.format(args_sql[0])
        if function.name == "ASCII":
            return CLICKHOUSE_CODE_POINT_TEMPLATE.format(args_sql[0])
        if function.name == "SUBSTR":
            if len(args_sql) == 3:
                return CLICKHOUSE_SUBSTRING_TEMPLATE.format(*args_sql)
            return f"substringUTF8(toString({args_sql[0]}), greatest({args_sql[1]}, 1))"
        args_sql[0] = f"toString({args_sql[0]})"
        name = CLICKHOUSE_TEXT_FUNCTION_NAMES.get(function.name, function.name)
        return f"{name}({','.join(args_sql)})"

    @staticmethod
    def render_cast_to(function: functions.CastTo, sql_context: SqlContext) -> str:
        """The cast with PostgreSQL's rules - to the column type of the target field, a time of day as
        its text; a cast PostgreSQL has none of is refused."""
        term_sql = function.get_arg_sql(function.args[0], sql_context)
        if function.target == CastType.TEXT:
            return ClickhouseValueTexts.get_text_sql(
                term_sql, function.source, is_aware=function.is_aware, max_length=function.parameters[0]
            )
        if ClickhouseValueCasts.is_refused(function):
            return ClickhouseValueCasts.get_refused_sql(term_sql, function)
        if function.target == CastType.TIME:
            return ClickhouseTemporalRenderers.get_time_text_sql(term_sql, "'UTC'")
        if function.target == CastType.DATETIME:
            return ClickhouseValueCasts.get_cast_sql(term_sql, function, CLICKHOUSE_DATETIME_COLUMN_TYPE)
        return ClickhouseValueCasts.get_cast_sql(
            term_sql, function, function.target_field.get_column_type(sql_context.dialect)
        )

    @staticmethod
    def render_address_text(function: AddressText, sql_context: SqlContext) -> str:
        """The text of an ``IPv6`` value - an IPv4 address held in it as its IPv4 text."""
        return CLICKHOUSE_ADDRESS_TEXT_SQL.format(value=function.get_arg_sql(function.args[0], sql_context))

    @staticmethod
    def render_boolean_as_text(function: functions.BooleanAsText, sql_context: SqlContext) -> str:
        """``'true'``/``'false'`` - a comparison is a number in ClickHouse, which its own text would show."""
        term_sql = function.get_arg_sql(function.args[0], sql_context)
        return f"(CASE WHEN {term_sql} IS NULL THEN NULL WHEN {term_sql} THEN 'true' ELSE 'false' END)"
