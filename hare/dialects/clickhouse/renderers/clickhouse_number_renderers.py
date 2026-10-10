from __future__ import annotations

from typing import TYPE_CHECKING

from hare.dialects.clickhouse.renderers.constants import CLICKHOUSE_MATH_FUNCTION_NAMES
from hare.sql import functions
from hare.sql.sql_context import SqlContext
from hare.sql.terms import functions as term_functions

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.renderers.term_renderers import TermRenderers


class ClickhouseNumberRenderers:
    """How ClickHouse writes numbers: random numbers, remainders of floats, rounding and the other
    math functions, and a float or a decimal written as text."""

    @classmethod
    def register(cls, renderers: TermRenderers) -> None:
        """Registers the number renderers on ClickHouse's renderers.

        Args:
            renderers: The renderers.
        """
        renderers.register(functions.RandomNumber, cls.render_random_number)
        renderers.register(term_functions.FloatMod, cls.render_float_mod)
        renderers.register(functions.MathFunction, cls.render_math_function)
        renderers.register(functions.Round, cls.render_round)
        renderers.register(functions.FloatAsText, cls.render_float_as_text)
        renderers.register(functions.DecimalAsText, cls.render_decimal_as_text)

    @staticmethod
    def render_random_number(function: functions.RandomNumber, sql_context: SqlContext) -> str:
        """A float from 0 to 1."""
        return "randCanonical()"

    @staticmethod
    def render_float_mod(function: term_functions.FloatMod, sql_context: SqlContext) -> str:
        """The remainder with the dividend's sign, as ``fmod``."""
        left_sql, right_sql = (f"toFloat64({function.get_arg_sql(arg, sql_context)})" for arg in function.args)
        return f"modulo({left_sql},{right_sql})"

    @staticmethod
    def render_math_function(function: functions.MathFunction, sql_context: SqlContext) -> str:
        args_sql = [function.get_arg_sql(arg, sql_context) for arg in function.args]
        if function.name == "LOG":
            # LOG(base, x) - ClickHouse's log() is the natural logarithm.
            base_sql, value_sql = args_sql
            return f"(log({value_sql})/log({base_sql}))"
        if function.name == "COT":
            return f"(1/tan({args_sql[0]}))"
        return f"{CLICKHOUSE_MATH_FUNCTION_NAMES.get(function.name, function.name)}({','.join(args_sql)})"

    @staticmethod
    def render_round(function: functions.Round, sql_context: SqlContext) -> str:
        """``round(x, places)``, half away from zero as PostgreSQL rounds a decimal."""
        term_sql, places_sql = (function.get_arg_sql(argument, sql_context) for argument in function.args)
        return f"round({term_sql},{places_sql})"

    @staticmethod
    def render_float_as_text(function: functions.FloatAsText, sql_context: SqlContext) -> str:
        return f"toString(toFloat64({function.get_arg_sql(function.args[0], sql_context)}))"

    @staticmethod
    def render_decimal_as_text(function: functions.DecimalAsText, sql_context: SqlContext) -> str:
        """The decimal at the field's scale, its trailing zeros kept."""
        term_sql = function.get_arg_sql(function.args[0], sql_context)
        return f"toDecimalString(toDecimal128({term_sql}, {function.scale}), {function.scale})"
