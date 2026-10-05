from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.dialects.sqlite.constants import (
    SQLITE_CAST_FUNCTION_NAME,
    SQLITE_LOWER_FUNCTION_NAME,
    SQLITE_TEXT_FUNCTION_PREFIX,
    SQLITE_UPPER_FUNCTION_NAME,
)
from hare.dialects.sqlite.renderers.constants import SQLITE_NATIVE_TEXT_FUNCTIONS
from hare.sql import functions
from hare.sql.sql_context import SqlContext
from hare.sql.terms.values.value_wrapper import ValueWrapper

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.renderers.term_renderers import TermRenderers
    from hare.sql.sql_context import SqlContext


class SqliteTextRenderers:
    """How SQLite writes text: the text functions, upper and lower case, concatenation, casts, and a
    boolean written as text."""

    @classmethod
    def register(cls, renderers: TermRenderers) -> None:
        """Registers the text renderers on SQLite's renderers.

        Args:
            renderers: The renderers.
        """
        renderers.register(functions.TextFunction, cls.render_text_function)
        renderers.register_name(functions.Upper, cls.get_upper_name)
        renderers.register_name(functions.Lower, cls.get_lower_name)
        renderers.register(functions.CastTo, cls.render_cast_to)
        renderers.register(functions.BooleanAsText, cls.render_boolean_as_text)

    @staticmethod
    def render_text_function(function: functions.TextFunction, sql_context: SqlContext) -> str:
        args_sql = [function.get_arg_sql(arg, sql_context) for arg in function.args]
        name = SQLITE_NATIVE_TEXT_FUNCTIONS.get(function.name, f"{SQLITE_TEXT_FUNCTION_PREFIX}{function.name.lower()}")
        return f"{name}({','.join(args_sql)})"

    @staticmethod
    def get_upper_name(function: Any, sql_context: SqlContext) -> str:
        # hare's function case-folds as Python's str.upper() does; SQLite's own UPPER() folds
        # ASCII only. Not in SQL stored in DDL or a migration file, where only native functions go.
        return "" if sql_context.native_functions_only else SQLITE_UPPER_FUNCTION_NAME

    @staticmethod
    def get_lower_name(function: Any, sql_context: SqlContext) -> str:
        return "" if sql_context.native_functions_only else SQLITE_LOWER_FUNCTION_NAME

    @staticmethod
    def render_cast_to(function: functions.CastTo, sql_context: SqlContext) -> str:
        term_sql = function.get_arg_sql(function.args[0], sql_context)
        arguments_sql = [
            ValueWrapper(function.target.value, allow_parametrize=False).get_sql(sql_context),
            ValueWrapper(function.source.value, allow_parametrize=False).get_sql(sql_context),
            *("NULL" if parameter is None else str(int(parameter)) for parameter in function.parameters),
            str(int(function.is_aware)),
        ]
        return f"{SQLITE_CAST_FUNCTION_NAME}({term_sql}, {', '.join(arguments_sql)})"

    @staticmethod
    def render_boolean_as_text(function: functions.BooleanAsText, sql_context: SqlContext) -> str:
        term_sql = function.get_arg_sql(function.args[0], sql_context)
        return f"CASE {term_sql} WHEN 1 THEN 'true' WHEN 0 THEN 'false' END"
