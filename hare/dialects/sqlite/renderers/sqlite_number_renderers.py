from __future__ import annotations

from typing import TYPE_CHECKING

from hare.dialects.sqlite.constants import (
    SQLITE_GREATEST_FUNCTION_NAME,
    SQLITE_LEAST_FUNCTION_NAME,
    SQLITE_MATH_FUNCTION_PREFIX,
    SQLITE_STATISTICS_FUNCTION_NAMES,
)
from hare.dialects.sqlite.renderers.constants import SQLITE_RANDOM_FLOAT_SQL
from hare.sql import analytics, functions
from hare.sql.sql_context import SqlContext
from hare.sql.terms import functions as term_functions
from hare.sql.terms.values.value_wrapper import ValueWrapper

if TYPE_CHECKING:
    from hare.dialects.base.renderers.term_renderers import TermRenderers


class SqliteNumberRenderers:
    """How SQLite writes numbers: statistics, random numbers, the greatest and least of values,
    remainders of integers and floats, the math functions, and a float written as text."""

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
        renderers.register(functions.MathFunction, cls.render_math_function)

    @staticmethod
    def render_statistic(function: functions.Statistic, sql_context: SqlContext) -> str:
        return function.get_statistic_sql(
            sql_context.note_connection_function(SQLITE_STATISTICS_FUNCTION_NAMES[function.name]), sql_context
        )

    @staticmethod
    def get_statistic_name(function: analytics.Statistic, sql_context: SqlContext) -> str:
        return sql_context.note_connection_function(SQLITE_STATISTICS_FUNCTION_NAMES[str(function.name)])

    @staticmethod
    def render_random_number(function: functions.RandomNumber, sql_context: SqlContext) -> str:
        return SQLITE_RANDOM_FLOAT_SQL

    @staticmethod
    def render_greatest_least(function: functions.GreatestLeast, sql_context: SqlContext) -> str:
        """``GREATEST()``/``LEAST()`` as hare's functions on the connection, comparing as numbers or as values.

        Args:
            function: The function's term.
            sql_context: How the SQL is written.

        Returns:
            The SQL.
        """
        args_sql = [function.get_arg_sql(arg, sql_context) for arg in function.args]
        name = sql_context.note_connection_function(
            SQLITE_GREATEST_FUNCTION_NAME if function.name == "GREATEST" else SQLITE_LEAST_FUNCTION_NAME
        )
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
        """SQLite's floating-point ``MOD()``."""
        return term_functions.Function.get_function_sql(function, sql_context)

    @staticmethod
    def render_math_function(function: functions.MathFunction, sql_context: SqlContext) -> str:
        args_sql = [function.get_arg_sql(arg, sql_context) for arg in function.args]
        name = sql_context.note_connection_function(f"{SQLITE_MATH_FUNCTION_PREFIX}{function.name.lower()}")
        return f"{name}({','.join(args_sql)})"
