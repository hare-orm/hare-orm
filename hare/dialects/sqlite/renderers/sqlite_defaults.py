from __future__ import annotations

from typing import TYPE_CHECKING

from hare.dialects.sqlite.constants import (
    SQLITE_NOW_LOCAL_NOW_FUNCTION_SQL_TEMPLATE,
    SQLITE_NOW_LOCAL_SQL,
    SQLITE_NOW_SQL_TEMPLATE,
    SQLITE_NOW_UTC_SQL,
)
from hare.dialects.sqlite.renderers.constants import (
    SQLITE_BARE_DEFAULT_PATTERN,
    SQLITE_NOW_DATE_SQL_TEMPLATE,
    SQLITE_NOW_LOCAL_MOMENT,
    SQLITE_NOW_SHIFTED_MOMENT_TEMPLATE,
    SQLITE_NOW_TIME_TEXT_FORMAT,
    SQLITE_RANDOM_HEX_SQL,
)
from hare.fields.db_defaults.now import Now
from hare.fields.db_defaults.random_hex import RandomHex
from hare.fields.db_defaults.sql_default import SqlDefault
from hare.fields.enums import NowValueType
from hare.time import Timezone

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.renderers.term_renderers import TermRenderers
    from hare.sql.sql_context import SqlContext


class SqliteDefaults:
    """How SQLite writes the database-level defaults of ``db_default``."""

    @classmethod
    def register(cls, renderers: TermRenderers) -> None:
        """Adds the renderers of ``SqlDefault`` and its subclasses to SQLite's term renderers.

        Args:
            renderers: SQLite's term renderers.
        """
        renderers.register(SqlDefault, cls.render_sql_default)
        renderers.register(Now, cls.render_now)
        renderers.register(RandomHex, cls.render_random_hex)

    @staticmethod
    def render_sql_default(default: SqlDefault, sql_context: SqlContext) -> str:
        # SQLite's DEFAULT takes a literal, a signed number or a parenthesized expression. A subclass
        # with no standard SQL of its own (UuidV7) refuses here.
        sql = default.get_standard_sql()
        if SQLITE_BARE_DEFAULT_PATTERN.fullmatch(sql) or default.is_parenthesized(sql):
            return sql
        return f"({sql})"

    @staticmethod
    def render_now(default: Now, sql_context: SqlContext) -> str:
        """The current moment as the text a Python-side value of the column is written as.

        Args:
            default: The default.
            sql_context: The rendering context.

        Returns:
            The SQL - SQLite's own date functions for the system's local zone or a zone with a
            constant offset, the ``SQLITE_LOCAL_NOW_FUNCTION_NAME`` function for a zone with DST.
        """
        if default.value_type == NowValueType.DATETIME:
            return SQLITE_NOW_UTC_SQL if Timezone.get_use_timezone() else SQLITE_NOW_LOCAL_SQL
        suffix = ""
        if Timezone.get_use_timezone():
            constant_offset = default.get_constant_utc_offset(Timezone.default())
            if constant_offset is None:
                return SQLITE_NOW_LOCAL_NOW_FUNCTION_SQL_TEMPLATE.format(
                    zone=default.get_quoted_text(Timezone.name()), value_type=default.value_type.value
                )
            moment = SQLITE_NOW_SHIFTED_MOMENT_TEMPLATE.format(minutes=int(constant_offset.total_seconds() // 60))
            suffix = " || " + default.get_quoted_text(default.get_configured_offset_text())
        else:
            moment = SQLITE_NOW_LOCAL_MOMENT
        if default.value_type == NowValueType.DATE:
            return SQLITE_NOW_DATE_SQL_TEMPLATE.format(moment=moment)
        return SQLITE_NOW_SQL_TEMPLATE.format(text_format=SQLITE_NOW_TIME_TEXT_FORMAT, moment=moment, suffix=suffix)

    @staticmethod
    def render_random_hex(default: RandomHex, sql_context: SqlContext) -> str:
        return SQLITE_RANDOM_HEX_SQL
