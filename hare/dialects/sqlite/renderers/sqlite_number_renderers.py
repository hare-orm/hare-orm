from __future__ import annotations

from typing import TYPE_CHECKING

from hare.dialects.sqlite.constants import (
    SQLITE_GREATEST_FUNCTION_NAME,
    SQLITE_LEAST_FUNCTION_NAME,
    SQLITE_MATH_FUNCTION_PREFIX,
    SQLITE_NUMBER_TEXT_FUNCTION_NAME,
    SQLITE_STATISTICS_FUNCTION_NAMES,
)
from hare.dialects.sqlite.renderers.constants import SQLITE_DECIMAL_MOD_MAX_SCALE, SQLITE_RANDOM_FLOAT_SQL
from hare.sql import analytics, functions
from hare.sql.sql_context import SqlContext
from hare.sql.terms import functions as term_functions
from hare.sql.terms.values.value_wrapper import ValueWrapper

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.renderers.term_renderers import TermRenderers
    from hare.sql.sql_context import SqlContext


class SqliteNumberRenderers:
    """How SQLite writes numbers: statistics, random numbers, the greatest and least of values,
    remainders of integers, floats and decimals, rounding and the other math functions, and a float
    or a decimal written as text."""

    @classmethod
    def register(cls, renderers: TermRenderers) -> None:
        """Registers the number renderers on SQLite's renderers.

        Args:
            renderers: The renderers.
        """
        renderers.register(functions.Statistic, cls.render_statistic)
        renderers.register_name(analytics.Statistic, cls.get_statistic_name)
        renderers.register(functions.RandomNumber, cls.render_random_number)
        renderers.register(functions.GreatestLeast, cls.render_greatest_least)
        renderers.register(term_functions.Mod, cls.render_mod)
        renderers.register(term_functions.FloatMod, cls.render_float_mod)
        renderers.register(term_functions.DecimalMod, cls.render_decimal_mod)
        renderers.register(functions.MathFunction, cls.render_math_function)
        renderers.register(functions.FloatAsText, cls.render_float_as_text)
        renderers.register(functions.DecimalAsText, cls.render_decimal_as_text)

    @staticmethod
    def render_statistic(function: functions.Statistic, sql_context: SqlContext) -> str:
        return function.get_statistic_sql(SQLITE_STATISTICS_FUNCTION_NAMES[function.name], sql_context)

    @staticmethod
    def get_statistic_name(function: analytics.Statistic, sql_context: SqlContext) -> str:
        return SQLITE_STATISTICS_FUNCTION_NAMES[str(function.name)]

    @staticmethod
    def render_random_number(function: functions.RandomNumber, sql_context: SqlContext) -> str:
        return SQLITE_RANDOM_FLOAT_SQL

    @staticmethod
    def render_greatest_least(function: functions.GreatestLeast, sql_context: SqlContext) -> str:
        args_sql = [function.get_arg_sql(arg, sql_context) for arg in function.args]
        name = SQLITE_GREATEST_FUNCTION_NAME if function.name == "GREATEST" else SQLITE_LEAST_FUNCTION_NAME
        comparison_type_sql = ValueWrapper(
            "number" if function.compares_numbers else "value", allow_parametrize=False
        ).get_sql(sql_context)
        return f"{name}({comparison_type_sql}, {', '.join(args_sql)})"

    @staticmethod
    def render_mod(function: term_functions.Mod, sql_context: SqlContext) -> str:
        """The integer ``%`` operator for integers - SQLite's ``MOD()`` computes in floating point."""
        if not function.integer:
            return term_functions.Function.get_function_sql(function, sql_context)
        left_sql, right_sql = (function.get_arg_sql(arg, sql_context) for arg in function.args)
        return f"(({left_sql})%({right_sql}))"

    @staticmethod
    def render_float_mod(function: term_functions.FloatMod, sql_context: SqlContext) -> str:
        """SQLite's floating-point ``MOD()``; ``+ 0.0`` turns a negative zero into PostgreSQL's positive one."""
        return f"({term_functions.Function.get_function_sql(function, sql_context)}+0.0)"

    @staticmethod
    def render_decimal_mod(function: term_functions.DecimalMod, sql_context: SqlContext) -> str:
        """Both operands scaled to whole numbers and the integer remainder scaled back - SQLite's
        ``MOD()`` runs on doubles. Beyond the scale a 64-bit integer holds, or with an unknown
        scale, it is ``MOD()`` itself."""
        if function.scale is None or function.scale > SQLITE_DECIMAL_MOD_MAX_SCALE:
            return term_functions.Function.get_function_sql(function, sql_context)
        factor = 10**function.scale
        left_sql, right_sql = (
            f"CAST(ROUND(({function.get_arg_sql(arg, sql_context)})*{factor}) AS INTEGER)" for arg in function.args
        )
        return f"(({left_sql}%{right_sql})/{factor}.0)"

    @staticmethod
    def render_math_function(function: functions.MathFunction, sql_context: SqlContext) -> str:
        args_sql = [function.get_arg_sql(arg, sql_context) for arg in function.args]
        return f"{SQLITE_MATH_FUNCTION_PREFIX}{function.name.lower()}({','.join(args_sql)})"

    @staticmethod
    def render_float_as_text(function: functions.FloatAsText, sql_context: SqlContext) -> str:
        return f"{SQLITE_NUMBER_TEXT_FUNCTION_NAME}({function.get_arg_sql(function.args[0], sql_context)},NULL)"

    @staticmethod
    def render_decimal_as_text(function: functions.DecimalAsText, sql_context: SqlContext) -> str:
        number_sql = function.get_arg_sql(function.args[0], sql_context)
        return f"{SQLITE_NUMBER_TEXT_FUNCTION_NAME}({number_sql},{function.scale})"
