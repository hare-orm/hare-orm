from __future__ import annotations

from typing import TYPE_CHECKING

from hare.dialects.postgresql.renderers.constants import (
    POSTGRESQL_NUMERIC_ONLY_MATH_FUNCTIONS,
    POSTGRESQL_RANDOM_FLOAT_SQL,
)
from hare.sql import functions
from hare.sql.sql_context import SqlContext
from hare.sql.terms import functions as term_functions

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.renderers.term_renderers import TermRenderers


class PostgresqlNumberRenderers:
    """How PostgreSQL writes numbers: statistics, random numbers, the greatest and least of values,
    remainders of integers, floats and decimals, rounding and the other math functions, and a float
    or a decimal written as text."""

    @classmethod
    def register(cls, renderers: TermRenderers) -> None:
        """Registers the number renderers on PostgreSQL's renderers.

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
        return POSTGRESQL_RANDOM_FLOAT_SQL

    @staticmethod
    def render_float_mod(function: term_functions.FloatMod, sql_context: SqlContext) -> str:
        """``x - y * TRUNC(x / y)`` - there's no ``MOD()`` for double precision."""
        left_sql, right_sql = (
            f"CAST({function.get_arg_sql(arg, sql_context)} AS DOUBLE PRECISION)" for arg in function.args
        )
        return f"({left_sql}-{right_sql}*TRUNC({left_sql}/{right_sql}))"

    @staticmethod
    def render_math_function(function: functions.MathFunction, sql_context: SqlContext) -> str:
        args_sql = [function.get_arg_sql(arg, sql_context) for arg in function.args]
        if function.name in POSTGRESQL_NUMERIC_ONLY_MATH_FUNCTIONS:
            # Only a numeric variant exists (MOD of fractions, LOG(base, x)).
            args_sql = [f"CAST({arg_sql} AS NUMERIC)" for arg_sql in args_sql]
        return f"{function.name}({','.join(args_sql)})"

    @staticmethod
    def render_round(function: functions.Round, sql_context: SqlContext) -> str:
        """``ROUND(x, places)`` - PostgreSQL has it for an exact decimal ``x`` only."""
        term_sql, places_sql = (function.get_arg_sql(argument, sql_context) for argument in function.args)
        return f"ROUND(CAST({term_sql} AS NUMERIC),{places_sql})"

    @staticmethod
    def render_float_as_text(function: functions.FloatAsText, sql_context: SqlContext) -> str:
        return f"CAST(CAST({function.get_arg_sql(function.args[0], sql_context)} AS DOUBLE PRECISION) AS TEXT)"

    @staticmethod
    def render_decimal_as_text(function: functions.DecimalAsText, sql_context: SqlContext) -> str:
        term_sql = function.get_arg_sql(function.args[0], sql_context)
        return f"CAST(ROUND(CAST({term_sql} AS NUMERIC),{function.scale}) AS TEXT)"
