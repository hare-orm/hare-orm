from __future__ import annotations

from typing import TYPE_CHECKING

from hare.dialects.clickhouse.renderers.constants import (
    CLICKHOUSE_FLOAT_ROUND_TEMPLATE,
    CLICKHOUSE_FLOAT_TEXT_TEMPLATE,
    CLICKHOUSE_MATH_DOMAIN_TEMPLATES,
    CLICKHOUSE_MATH_FUNCTION_NAMES,
    CLICKHOUSE_QUOTIENT_SCALE,
)
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
        renderers.register(term_functions.Mod, cls.render_mod)
        renderers.register(term_functions.FloatMod, cls.render_float_mod)
        renderers.register(term_functions.DecimalMod, cls.render_decimal_mod)
        renderers.register(functions.MathFunction, cls.render_math_function)
        renderers.register(functions.Round, cls.render_round)
        renderers.register(functions.FloatAsText, cls.render_float_as_text)
        renderers.register(functions.DecimalAsText, cls.render_decimal_as_text)

    @staticmethod
    def render_random_number(function: functions.RandomNumber, sql_context: SqlContext) -> str:
        """A float from 0 to 1."""
        return "randCanonical()"

    @staticmethod
    def render_mod(function: term_functions.Mod, sql_context: SqlContext) -> str:
        """The remainder of integers with the dividend's sign, as PostgreSQL's - the dividend less its
        quotient rounded towards zero; ClickHouse's ``modulo()`` of a column by a negative divisor
        gives the other sign (``-5 % -2`` is 1)."""
        if not function.integer:
            return term_functions.Function.get_function_sql(function, sql_context)
        left_sql, right_sql = (function.get_arg_sql(arg, sql_context) for arg in function.args)
        return f"(({left_sql})-intDiv(({left_sql}),({right_sql}))*({right_sql}))"

    @staticmethod
    def render_float_mod(function: term_functions.FloatMod, sql_context: SqlContext) -> str:
        """The remainder with the dividend's sign, as ``fmod``."""
        left_sql, right_sql = (f"toFloat64({function.get_arg_sql(arg, sql_context)})" for arg in function.args)
        return f"modulo({left_sql},{right_sql})"

    @staticmethod
    def render_decimal_mod(function: term_functions.DecimalMod, sql_context: SqlContext) -> str:
        """Both operands at one scale - ClickHouse's ``modulo()`` of two Decimals reads them at the
        dividend's scale."""
        scale = CLICKHOUSE_QUOTIENT_SCALE if function.scale is None else function.scale
        left_sql, right_sql = (
            f"toDecimal256({function.get_arg_sql(arg, sql_context)}, {scale})" for arg in function.args
        )
        return f"modulo({left_sql},{right_sql})"

    @staticmethod
    def render_math_function(function: functions.MathFunction, sql_context: SqlContext) -> str:
        """The function - one defined for part of its arguments refuses the others as PostgreSQL does,
        ``LOG(base, x)`` by binary logarithms."""
        args_sql = [function.get_arg_sql(arg, sql_context) for arg in function.args]
        domain_template = CLICKHOUSE_MATH_DOMAIN_TEMPLATES.get(function.name)
        if domain_template is not None:
            return domain_template.format(*args_sql)
        if function.name == "COT":
            return f"(1/tan({args_sql[0]}))"
        return f"{CLICKHOUSE_MATH_FUNCTION_NAMES.get(function.name, function.name)}({','.join(args_sql)})"

    @staticmethod
    def render_round(function: functions.Round, sql_context: SqlContext) -> str:
        """``round(x, places)`` - half away from zero, as PostgreSQL rounds a decimal and SQLite a
        float; ClickHouse rounds a float's half to even."""
        term_sql, places_sql = (function.get_arg_sql(argument, sql_context) for argument in function.args)
        if function.rounds_float:
            return CLICKHOUSE_FLOAT_ROUND_TEMPLATE.format(term_sql, places_sql, repr(float(10**function.precision)))
        return f"round({term_sql},{places_sql})"

    @staticmethod
    def render_float_as_text(function: functions.FloatAsText, sql_context: SqlContext) -> str:
        """The float as PostgreSQL writes it - ``1e+15``, ``Infinity``, ``NaN``."""
        return CLICKHOUSE_FLOAT_TEXT_TEMPLATE.format(
            f"toFloat64({function.get_arg_sql(function.args[0], sql_context)})"
        )

    @staticmethod
    def render_decimal_as_text(function: functions.DecimalAsText, sql_context: SqlContext) -> str:
        """The decimal at the field's scale, its trailing zeros kept."""
        term_sql = function.get_arg_sql(function.args[0], sql_context)
        return f"toDecimalString(toDecimal128({term_sql}, {function.scale}), {function.scale})"
