from __future__ import annotations

from typing import TYPE_CHECKING

from hare.dialects.postgresql.renderers.constants import POSTGRESQL_DIGEST_FUNCTIONS
from hare.dialects.postgresql.renderers.postgresql_temporal_renderers import PostgresqlTemporalRenderers
from hare.sql import functions
from hare.sql.enums import CastType
from hare.sql.sql_context import SqlContext

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.renderers.term_renderers import TermRenderers


class PostgresqlTextRenderers:
    """How PostgreSQL writes text: the text functions, upper and lower case, concatenation, casts, and
    a boolean written as text."""

    @classmethod
    def register(cls, renderers: TermRenderers) -> None:
        """Registers the text renderers on PostgreSQL's renderers.

        Args:
            renderers: The renderers.
        """
        renderers.register(functions.TextFunction, cls.render_text_function)
        renderers.register(functions.Concat, cls.render_concat)
        renderers.register(functions.CastTo, cls.render_cast_to)
        renderers.register(functions.BooleanAsText, cls.render_boolean_as_text)

    @staticmethod
    def render_text_function(function: functions.TextFunction, sql_context: SqlContext) -> str:
        """The function with its first argument read as text."""
        args_sql = [function.get_arg_sql(arg, sql_context) for arg in function.args]
        # CHR takes an integer code point - there's no bigint variant.
        args_sql[0] = f"CAST({args_sql[0]} AS {'INTEGER' if function.name == 'CHR' else 'TEXT'})"
        if function.name in POSTGRESQL_DIGEST_FUNCTIONS:
            return f"ENCODE({function.name}(CONVERT_TO({args_sql[0]}, 'UTF8')), 'hex')"
        if function.name == "SHA1":
            return f"ENCODE(DIGEST(CONVERT_TO({args_sql[0]}, 'UTF8'), 'sha1'), 'hex')"
        return f"{function.name}({','.join(args_sql)})"

    @staticmethod
    def render_concat(function: functions.Concat, sql_context: SqlContext) -> str:
        """``CONCAT()`` - it reads a NULL argument as empty text."""
        return f"CONCAT({','.join(function.get_arg_sql(argument, sql_context) for argument in function.args)})"

    @classmethod
    def render_cast_to(cls, function: functions.CastTo, sql_context: SqlContext) -> str:
        """The cast - a naive timestamp read or made in the machine's local zone."""
        term_sql = function.get_arg_sql(function.args[0], sql_context)
        if not function.is_aware and (function.source == CastType.DATETIME) != (function.target == CastType.DATETIME):
            zone_sql = PostgresqlTemporalRenderers.get_local_zone_sql(sql_context)
            if function.target == CastType.DATETIME:
                return f"(CAST({term_sql} AS TIMESTAMP) AT TIME ZONE {zone_sql})"
            term_sql = f"(CAST({term_sql} AS TIMESTAMPTZ) AT TIME ZONE {zone_sql})"
        # A time of day is cast without its offset.
        target_type = (
            "TIME" if function.target == CastType.TIME else function.target_field.get_column_type(sql_context.dialect)
        )
        return f"CAST({term_sql} AS {target_type})"

    @staticmethod
    def render_boolean_as_text(function: functions.BooleanAsText, sql_context: SqlContext) -> str:
        return f"CAST({function.get_arg_sql(function.args[0], sql_context)} AS TEXT)"
