from __future__ import annotations

from typing import TYPE_CHECKING

from hare.dialects.clickhouse.renderers.constants import (
    CLICKHOUSE_MATH_FUNCTION_NAMES,
    CLICKHOUSE_MATH_FUNCTION_TEMPLATES,
    DECIMAL_QUOTIENT_SCALE,
)
from hare.sql import functions
from hare.sql.sql_context import SqlContext
from hare.sql.terms import functions as term_functions

if TYPE_CHECKING:
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
        renderers.register(term_functions.Mod, cls.render_mod)
        renderers.register(term_functions.FloatMod, cls.render_float_mod)
        renderers.register(term_functions.DecimalMod, cls.render_decimal_mod)
        renderers.register(functions.MathFunction, cls.render_math_function)
        renderers.register(functions.Round, cls.render_round)

    @staticmethod
    def render_random_number(function: functions.RandomNumber, sql_context: SqlContext) -> str:
        """A float from 0 to 1."""
        return "randCanonical()"

    @staticmethod
    def render_mod(function: term_functions.Mod, sql_context: SqlContext) -> str:
        """``modulo()``."""
        left_sql, right_sql = (function.get_arg_sql(arg, sql_context) for arg in function.args)
        return f"modulo({left_sql},{right_sql})"

    @staticmethod
    def render_float_mod(function: term_functions.FloatMod, sql_context: SqlContext) -> str:
        """The remainder with the dividend's sign, as ``fmod``."""
        left_sql, right_sql = (f"toFloat64({function.get_arg_sql(arg, sql_context)})" for arg in function.args)
        return f"modulo({left_sql},{right_sql})"

    @staticmethod
    def render_decimal_mod(function: term_functions.DecimalMod, sql_context: SqlContext) -> str:
        """Both operands at one scale - ClickHouse's ``modulo()`` of two Decimals reads them at the
        dividend's scale."""
        scale = DECIMAL_QUOTIENT_SCALE if function.scale is None else function.scale
        left_sql, right_sql = (
            f"toDecimal256({function.get_arg_sql(arg, sql_context)}, {scale})" for arg in function.args
        )
        return f"modulo({left_sql},{right_sql})"

    @staticmethod
    def render_math_function(function: functions.MathFunction, sql_context: SqlContext) -> str:
        """ClickHouse's own function, or its SQL of one it lacks."""
        args_sql = [function.get_arg_sql(arg, sql_context) for arg in function.args]
        template = CLICKHOUSE_MATH_FUNCTION_TEMPLATES.get(function.name)
        if template is not None:
            return template.format(*args_sql)
        return f"{CLICKHOUSE_MATH_FUNCTION_NAMES.get(function.name, function.name)}({','.join(args_sql)})"

    @staticmethod
    def render_round(function: functions.Round, sql_context: SqlContext) -> str:
        """``round(x, places)``."""
        term_sql, places_sql = (function.get_arg_sql(argument, sql_context) for argument in function.args)
        return f"round({term_sql},{places_sql})"
