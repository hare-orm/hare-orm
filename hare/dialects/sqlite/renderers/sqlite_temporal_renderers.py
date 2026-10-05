from __future__ import annotations

from typing import TYPE_CHECKING

from hare.dialects.sqlite.constants import (
    SQLITE_DATE_DIFFERENCE_FUNCTION_NAME,
    SQLITE_DATE_SHIFT_FUNCTION_NAME,
    SQLITE_DATE_TIMESTAMP_FUNCTION_NAME,
    SQLITE_DATETIME_DIFFERENCE_FUNCTION_NAME,
    SQLITE_DATETIME_SHIFT_FUNCTION_NAME,
    SQLITE_EXTRACT_FUNCTION_NAME,
    SQLITE_TRUNC_FUNCTION_NAME,
)
from hare.dialects.sqlite.renderers.sqlite_defaults import SqliteDefaults
from hare.fields.db_defaults.now import Now
from hare.sql import functions
from hare.sql.exceptions import FunctionException
from hare.sql.sql_context import SqlContext
from hare.sql.terms import functions as term_functions
from hare.sql.terms.values.value_wrapper import ValueWrapper

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.renderers.term_renderers import TermRenderers
    from hare.sql.sql_context import SqlContext


class SqliteTemporalRenderers:
    """How SQLite writes dates and times: the current moment and date, a part extracted, a value
    truncated, cast, shifted by an interval or subtracted, a date compared with a timestamp, and a
    moment written as text."""

    @classmethod
    def register(cls, renderers: TermRenderers) -> None:
        """Registers the temporal renderers on SQLite's renderers.

        Args:
            renderers: The renderers.
        """
        renderers.register(functions.Date, cls.render_date)
        renderers.register(functions.Now, cls.render_now)
        renderers.register(functions.Extract, cls.render_extract)
        renderers.register(functions.DateTrunc, cls.render_date_trunc)
        renderers.register(functions.DatetimeCast, cls.render_datetime_cast)
        renderers.register(functions.TemporalShift, cls.render_temporal_shift)
        renderers.register(functions.TemporalDifference, cls.render_temporal_difference)
        renderers.register(functions.DateAsTimestamp, cls.render_date_as_timestamp)
        renderers.register(term_functions.Interval, cls.render_interval)
        renderers.register(functions.DatetimeAsText, cls.render_datetime_as_text)

    @staticmethod
    def render_date(function: functions.Date, sql_context: SqlContext) -> str:
        # A CAST to DATE takes numeric affinity on SQLite; date() keeps the ISO text a date is stored as.
        return f"DATE({function.get_arg_sql(function.args[0], sql_context)})"

    @staticmethod
    def render_now(function: functions.Now, sql_context: SqlContext) -> str:
        # SQLite has no NOW(): the current moment as the text a DatetimeField value is written as.
        return SqliteDefaults.render_now(Now(), sql_context)

    @staticmethod
    def render_extract(function: functions.Extract, sql_context: SqlContext) -> str:
        # SQLite has no EXTRACT() and no IANA zones - a function extracts in the zone itself.
        date_part_sql = ValueWrapper(function.date_part.value).get_sql(sql_context)
        field_sql = function.field.get_sql(sql_context)
        zone_sql = ValueWrapper(function.zone_name).get_sql(sql_context)
        return f"{SQLITE_EXTRACT_FUNCTION_NAME}({date_part_sql}, {field_sql}, {zone_sql})"

    @staticmethod
    def render_date_trunc(function: functions.DateTrunc, sql_context: SqlContext) -> str:
        field_sql = function.field.get_sql(sql_context)
        trunc_type_sql = ValueWrapper(function.trunc_type.value, allow_parametrize=False).get_sql(sql_context)
        zone_sql = ValueWrapper(function.zone_name).get_sql(sql_context)
        return f"{SQLITE_TRUNC_FUNCTION_NAME}({trunc_type_sql}, {field_sql}, {zone_sql})"

    @staticmethod
    def render_datetime_cast(function: functions.DatetimeCast, sql_context: SqlContext) -> str:
        field_sql = function.field.get_sql(sql_context)
        target_sql = ValueWrapper(function.target.value).get_sql(sql_context)
        zone_sql = ValueWrapper(function.zone_name).get_sql(sql_context)
        return f"{SQLITE_EXTRACT_FUNCTION_NAME}({target_sql}, {field_sql}, {zone_sql})"

    @staticmethod
    def render_temporal_shift(function: functions.TemporalShift, sql_context: SqlContext) -> str:
        if sql_context.native_functions_only:
            raise FunctionException("Date/datetime arithmetic has no dialect-neutral SQL form on SQLite")
        base_term, microseconds_term = function.args
        function_name = SQLITE_DATE_SHIFT_FUNCTION_NAME if function.is_date else SQLITE_DATETIME_SHIFT_FUNCTION_NAME
        sign = -1 if function.is_subtraction else 1
        base_sql = function.get_arg_sql(base_term, sql_context)
        return f"{function_name}({base_sql},{function.get_arg_sql(microseconds_term, sql_context)},{sign})"

    @staticmethod
    def render_temporal_difference(function: functions.TemporalDifference, sql_context: SqlContext) -> str:
        if sql_context.native_functions_only:
            raise FunctionException("Date/datetime arithmetic has no dialect-neutral SQL form on SQLite")
        left_term, right_term = function.args
        function_name = (
            SQLITE_DATE_DIFFERENCE_FUNCTION_NAME if function.is_date else SQLITE_DATETIME_DIFFERENCE_FUNCTION_NAME
        )
        left_sql = function.get_arg_sql(left_term, sql_context)
        right_sql = function.get_arg_sql(right_term, sql_context)
        return f"{function_name}({left_sql},{right_sql})"

    @staticmethod
    def render_date_as_timestamp(function: functions.DateAsTimestamp, sql_context: SqlContext) -> str:
        term_sql = function.get_arg_sql(function.args[0], sql_context)
        zone_sql = (
            "NULL"
            if function.zone_name is None
            else ValueWrapper(function.zone_name, allow_parametrize=False).get_sql(sql_context)
        )
        return f"{SQLITE_DATE_TIMESTAMP_FUNCTION_NAME}({term_sql},{zone_sql})"

    @staticmethod
    def render_interval(interval: term_functions.Interval, sql_context: SqlContext) -> str:
        raise FunctionException(
            "Interval(...) has no SQLite rendering - SQLite has no INTERVAL literal; build "
            "the equivalent date()/datetime() modifier expression directly for this dialect."
        )

    @staticmethod
    def render_datetime_as_text(function: functions.DatetimeAsText, sql_context: SqlContext) -> str:
        # A timestamp column already holds this text.
        return function.get_arg_sql(function.args[0], sql_context)
