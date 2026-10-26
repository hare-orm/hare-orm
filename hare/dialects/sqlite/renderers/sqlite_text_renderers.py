from __future__ import annotations

from typing import TYPE_CHECKING

from hare.dialects.sqlite.constants import (
    SQLITE_BOOLEAN_CAST_TEMPLATE,
    SQLITE_CAST_FUNCTION_NAME,
    SQLITE_CAST_TYPE_NAMES,
    SQLITE_TEXT_FUNCTION_PREFIX,
)
from hare.dialects.sqlite.renderers.constants import SQLITE_NATIVE_TEXT_FUNCTIONS
from hare.sql import functions
from hare.sql.enums import CastType
from hare.sql.sql_context import SqlContext
from hare.sql.terms import criteria
from hare.sql.terms.values.value_wrapper import ValueWrapper

if TYPE_CHECKING:
    from hare.dialects.base.renderers.term_renderers import TermRenderers


class SqliteTextRenderers:
    """How SQLite writes text: the text functions, concatenation, casts, and a boolean written as text."""

    @classmethod
    def register(cls, renderers: TermRenderers) -> None:
        """Registers the text renderers on SQLite's renderers.

        Args:
            renderers: The renderers.
        """
        renderers.register(functions.TextFunction, cls.render_text_function)
        renderers.register(functions.CastTo, cls.render_cast_to)

    @staticmethod
    def render_text_function(function: functions.TextFunction, sql_context: SqlContext) -> str:
        args_sql = [function.get_arg_sql(arg, sql_context) for arg in function.args]
        name = SQLITE_NATIVE_TEXT_FUNCTIONS.get(function.name)
        if name is None:
            name = sql_context.note_connection_function(f"{SQLITE_TEXT_FUNCTION_PREFIX}{function.name.lower()}")
        return f"{name}({','.join(args_sql)})"

    @staticmethod
    def render_cast_to(function: functions.CastTo, sql_context: SqlContext) -> str:
        """SQLite's own ``CAST`` to text, a number or a boolean; hare's cast to a date/time, which reads
        its stored text - a JSON null of a path cast to it is NULL."""
        argument = function.args[0]
        term_sql = function.get_arg_sql(argument, sql_context)
        if function.target == CastType.TEXT:
            return f"CAST({term_sql} AS TEXT)"
        if isinstance(argument, criteria.JSONAttributeCriterion) and not argument.as_text:
            # A path's value as JSON writes a JSON null as the text null - a string is quoted.
            term_sql = f"NULLIF({term_sql}, 'null')"
        type_name = SQLITE_CAST_TYPE_NAMES.get(function.target)
        if type_name is not None:
            return f"CAST({term_sql} AS {type_name})"
        if function.target == CastType.BOOLEAN:
            return SQLITE_BOOLEAN_CAST_TEMPLATE.format(term_sql)
        arguments_sql = [
            ValueWrapper(function.target.value, allow_parametrize=False).get_sql(sql_context),
            ValueWrapper(function.source.value, allow_parametrize=False).get_sql(sql_context),
            str(int(function.is_aware)),
        ]
        name = sql_context.note_connection_function(SQLITE_CAST_FUNCTION_NAME)
        return f"{name}({term_sql}, {', '.join(arguments_sql)})"
