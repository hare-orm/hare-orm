from __future__ import annotations

from typing import TYPE_CHECKING

from hare.dialects.clickhouse.constants import CLICKHOUSE_DATETIME_COLUMN_TYPE
from hare.dialects.clickhouse.lookups.constants import CLICKHOUSE_ADDRESS_TEXT_SQL
from hare.dialects.clickhouse.renderers.clickhouse_temporal_renderers import ClickhouseTemporalRenderers
from hare.dialects.clickhouse.renderers.clickhouse_value_casts import ClickhouseValueCasts
from hare.dialects.clickhouse.renderers.constants import (
    CLICKHOUSE_CHARACTER_TEMPLATE,
    CLICKHOUSE_CODE_POINT_TEMPLATE,
    CLICKHOUSE_DIGEST_FUNCTIONS,
    CLICKHOUSE_GENERIC_FUNCTION_NAMES,
    CLICKHOUSE_SIGNED_RESULT_TEMPLATE,
    CLICKHOUSE_TEXT_FUNCTION_NAMES,
    CLICKHOUSE_TIME_OF_TEXT_TEMPLATE,
    CLICKHOUSE_UNSIGNED_RESULT_FUNCTION_NAMES,
    CLICKHOUSE_UTC_ZONE_SQL,
)
from hare.sql import functions
from hare.sql.enums import CastType
from hare.sql.functions.address_text import AddressText
from hare.sql.sql_context import SqlContext

if TYPE_CHECKING:
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
        renderers.register(functions.Collate, cls.render_collate)
        renderers.register(AddressText, cls.render_address_text)
        for function_name in CLICKHOUSE_GENERIC_FUNCTION_NAMES:
            renderers.register_function(function_name, cls.render_generic_function)

    @staticmethod
    def render_generic_function(function: Function, sql_context: SqlContext) -> str:
        """A plain SQL function under ClickHouse's own name."""
        args_sql = ",".join(function.get_arg_sql(argument, sql_context) for argument in function.args)
        function_sql = f"{CLICKHOUSE_GENERIC_FUNCTION_NAMES[function.name]}({args_sql})"
        if function.name in CLICKHOUSE_UNSIGNED_RESULT_FUNCTION_NAMES:
            return CLICKHOUSE_SIGNED_RESULT_TEMPLATE.format(function_sql)
        return function_sql

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
        args_sql[0] = f"toString({args_sql[0]})"
        name = CLICKHOUSE_TEXT_FUNCTION_NAMES.get(function.name, function.name)
        function_sql = f"{name}({','.join(args_sql)})"
        if function.name in CLICKHOUSE_UNSIGNED_RESULT_FUNCTION_NAMES:
            return CLICKHOUSE_SIGNED_RESULT_TEMPLATE.format(function_sql)
        return function_sql

    @staticmethod
    def render_collate(function: functions.Collate, sql_context: SqlContext) -> str:
        """``value COLLATE 'name'`` - a part of an ``ORDER BY`` key on ClickHouse, not an expression of its
        own, so written without the parentheses an expression gets."""
        term_sql = function.get_arg_sql(function.args[0], sql_context)
        collation_sql = (
            sql_context.dialect.literals.get_collation_sql(function.collation)
            if function.names_collation
            else function.collation
        )
        return f"{term_sql} COLLATE {collation_sql}"

    @staticmethod
    def render_cast_to(function: functions.CastTo, sql_context: SqlContext) -> str:
        """The cast as ClickHouse converts - to the column type of the target field, a time of day as
        its text, to text by ``toString()``."""
        term_sql = function.get_arg_sql(function.args[0], sql_context)
        # A naive moment (use_timezone=False) is the instant of its local wall clock.
        zone_sql = ClickhouseTemporalRenderers.get_moment_zone_sql(function.is_aware, sql_context)
        if function.target == CastType.TEXT:
            if function.source == CastType.DATETIME:
                return f"toString({term_sql}, {zone_sql})"
            return f"toString({term_sql})"
        if function.target == CastType.TIME:
            # A time of day is kept as its text - a time is cast as it is, a text read as one first.
            if function.source == CastType.TIME:
                return term_sql
            if function.source == CastType.TEXT:
                term_sql = CLICKHOUSE_TIME_OF_TEXT_TEMPLATE.format(term_sql)
            elif function.source == CastType.UNKNOWN:
                # An expression of no known field - a text function, a literal - read as a text.
                term_sql = CLICKHOUSE_TIME_OF_TEXT_TEMPLATE.format(f"toString({term_sql})")
            time_zone_sql = zone_sql if function.source == CastType.DATETIME else CLICKHOUSE_UTC_ZONE_SQL
            return ClickhouseTemporalRenderers.get_time_text_sql(term_sql, time_zone_sql)
        if function.target == CastType.DATETIME:
            return ClickhouseValueCasts.get_cast_sql(term_sql, function, CLICKHOUSE_DATETIME_COLUMN_TYPE, zone_sql)
        return ClickhouseValueCasts.get_cast_sql(
            term_sql, function, function.target_field.get_column_type(sql_context.dialect), zone_sql
        )

    @staticmethod
    def render_address_text(function: AddressText, sql_context: SqlContext) -> str:
        """The text of an ``IPv6`` value - an IPv4 address held in it as its IPv4 text."""
        return CLICKHOUSE_ADDRESS_TEXT_SQL.format(value=function.get_arg_sql(function.args[0], sql_context))
