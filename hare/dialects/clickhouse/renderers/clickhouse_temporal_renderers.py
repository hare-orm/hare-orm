from __future__ import annotations

from typing import TYPE_CHECKING

from hare.dialects.clickhouse.constants import CLICKHOUSE_DATETIME_PRECISION
from hare.dialects.clickhouse.renderers.constants import (
    CLICKHOUSE_DATE_PART_FUNCTIONS,
    CLICKHOUSE_DATETIME_TEXT_FORMAT,
    CLICKHOUSE_TIME_TEXT_FORMAT,
    CLICKHOUSE_WHOLE_SECOND_FRACTION_PATTERN,
)
from hare.sql import functions
from hare.sql.constants import MICROSECONDS_PER_DAY
from hare.sql.enums import DatetimeCastTarget, DateTruncSource, TruncType
from hare.sql.sql_context import SqlContext
from hare.sql.terms import functions as term_functions

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.renderers.term_renderers import TermRenderers


class ClickhouseTemporalRenderers:
    """How ClickHouse writes dates and times: a part extracted, a value truncated, cast, shifted by
    microseconds or subtracted, a date read as a moment, and a moment written as text. A moment is a
    ``DateTime64`` in UTC; a zone, when one is given, reads it as that zone's wall clock."""

    @classmethod
    def register(cls, renderers: TermRenderers) -> None:
        """Registers the temporal renderers on ClickHouse's renderers.

        Args:
            renderers: The renderers.
        """
        renderers.register(functions.Extract, cls.render_extract)
        renderers.register(functions.DateTrunc, cls.render_date_trunc)
        renderers.register(functions.DatetimeCast, cls.render_datetime_cast)
        renderers.register(functions.TemporalShift, cls.render_temporal_shift)
        renderers.register(functions.TemporalDifference, cls.render_temporal_difference)
        renderers.register(functions.DateAsTimestamp, cls.render_date_as_timestamp)
        renderers.register(term_functions.Interval, cls.render_interval)
        renderers.register(functions.DatetimeAsText, cls.render_datetime_as_text)

    @staticmethod
    def get_zone_sql(zone_name: str, sql_context: SqlContext) -> str:
        """A zone as a literal - a function reading the moment in it takes no parameter there.

        Args:
            zone_name: A validated IANA name.
            sql_context: The context.

        Returns:
            The literal.
        """
        return sql_context.dialect.literals.get_string_literal_sql(zone_name)

    @classmethod
    def get_zoned_sql(cls, term_sql: str, zone_name: str | None, sql_context: SqlContext) -> str:
        """A moment read in a zone, None for its own UTC.

        Args:
            term_sql: The moment.
            zone_name: The zone.
            sql_context: The context.

        Returns:
            The SQL.
        """
        if zone_name is None:
            return term_sql
        return f"toTimeZone({term_sql}, {cls.get_zone_sql(zone_name, sql_context)})"

    @staticmethod
    def get_time_text_sql(moment_sql: str, zone_sql: str) -> str:
        """A moment's time of day at a zone, as ``datetime.time.isoformat()`` writes it - its
        microseconds only when it has any.

        Args:
            moment_sql: The moment.
            zone_sql: The zone's literal.

        Returns:
            The SQL.
        """
        text_sql = f"formatDateTime({moment_sql}, '{CLICKHOUSE_TIME_TEXT_FORMAT}', {zone_sql})"
        return f"replaceRegexpOne({text_sql}, '{CLICKHOUSE_WHOLE_SECOND_FRACTION_PATTERN}', '')"

    @classmethod
    def render_extract(cls, function: functions.Extract, sql_context: SqlContext) -> str:
        """The part, in Python's ``datetime`` meaning."""
        source_sql = cls.get_zoned_sql(function.field.get_sql(sql_context), function.zone_name, sql_context)
        return CLICKHOUSE_DATE_PART_FUNCTIONS[str(function.date_part)].format(source_sql)

    @classmethod
    def render_date_trunc(cls, function: functions.DateTrunc, sql_context: SqlContext) -> str:
        """``dateTrunc``; a time of day is truncated in its text."""
        field_sql = function.field.get_sql(sql_context)
        unit_sql = sql_context.dialect.literals.get_string_literal_sql(function.trunc_type.value)
        if function.source == DateTruncSource.DATETIME:
            zone_sql = cls.get_zone_sql(function.zone_name or "UTC", sql_context)
            if function.trunc_type == TruncType.DATE:
                return f"toDate32({field_sql}, {zone_sql})"
            if function.trunc_type == TruncType.TIME:
                return cls.get_time_text_sql(field_sql, zone_sql)
            # The moment is truncated as it is stored - only the result is given its precision.
            truncated_sql = f"dateTrunc({unit_sql}, toTimeZone({field_sql}, {zone_sql}))"
            return f"toDateTime64({truncated_sql}, {CLICKHOUSE_DATETIME_PRECISION}, 'UTC')"
        if function.source == DateTruncSource.DATE:
            if function.trunc_type == TruncType.DATE:
                return field_sql
            return f"toDate32(dateTrunc({unit_sql}, toDateTime64({field_sql}, 0, 'UTC')))"
        if function.trunc_type == TruncType.TIME:
            return field_sql
        moment_sql = (
            f"parseDateTime64BestEffort(concat('1970-01-01 ', {field_sql}), {CLICKHOUSE_DATETIME_PRECISION}, 'UTC')"
        )
        return cls.get_time_text_sql(f"dateTrunc({unit_sql}, {moment_sql})", "'UTC'")

    @classmethod
    def render_datetime_cast(cls, function: functions.DatetimeCast, sql_context: SqlContext) -> str:
        """The date or the time of day of a moment, at its zone."""
        zone_sql = cls.get_zone_sql(function.zone_name or "UTC", sql_context)
        field_sql = function.field.get_sql(sql_context)
        if function.target == DatetimeCastTarget.DATE:
            return f"toDate32({field_sql}, {zone_sql})"
        return cls.get_time_text_sql(field_sql, zone_sql)

    @staticmethod
    def render_temporal_shift(function: functions.TemporalShift, sql_context: SqlContext) -> str:
        """The moment or the date shifted by whole microseconds - never a calendar day."""
        base_term, microseconds_term = function.args
        base_sql = function.get_arg_sql(base_term, sql_context)
        microseconds_sql = f"toInt64({function.get_arg_sql(microseconds_term, sql_context)})"
        shift = "subtractMicroseconds" if function.is_subtraction else "addMicroseconds"
        moment_sql = f"toDateTime64({base_sql}, {CLICKHOUSE_DATETIME_PRECISION}, 'UTC')"
        shifted_sql = f"{shift}({moment_sql}, {microseconds_sql})"
        if function.is_date:
            return f"toDate32({shifted_sql}, 'UTC')"
        return shifted_sql

    @staticmethod
    def render_temporal_difference(function: functions.TemporalDifference, sql_context: SqlContext) -> str:
        """The microseconds from the right moment to the left one; whole days of dates."""
        left_term, right_term = function.args
        left_sql = function.get_arg_sql(left_term, sql_context)
        right_sql = function.get_arg_sql(right_term, sql_context)
        if function.is_date:
            return f"(dateDiff('day', toDate32({right_sql}), toDate32({left_sql})) * {MICROSECONDS_PER_DAY})"
        precision = CLICKHOUSE_DATETIME_PRECISION
        return (
            f"(toUnixTimestamp64Micro(toDateTime64({left_sql}, {precision}, 'UTC'))"
            f" - toUnixTimestamp64Micro(toDateTime64({right_sql}, {precision}, 'UTC')))"
        )

    @classmethod
    def render_date_as_timestamp(cls, function: functions.DateAsTimestamp, sql_context: SqlContext) -> str:
        """The date's midnight in its zone, as a moment."""
        term_sql = function.get_arg_sql(function.args[0], sql_context)
        zone_sql = cls.get_zone_sql(function.zone_name or "UTC", sql_context)
        return f"toDateTime64(toDate32({term_sql}), {CLICKHOUSE_DATETIME_PRECISION}, {zone_sql})"

    @staticmethod
    def render_interval(interval: term_functions.Interval, sql_context: SqlContext) -> str:
        expression, unit = interval.get_expression_and_unit()
        return f"INTERVAL {expression} {unit}"

    @staticmethod
    def render_datetime_as_text(function: functions.DatetimeAsText, sql_context: SqlContext) -> str:
        """The text SQLite stores a moment in - an aware one in UTC with its offset, a naive one as
        its wall clock; no fraction for a whole second."""
        term_sql = function.get_arg_sql(function.args[0], sql_context)
        text_sql = (
            f"replaceRegexpOne(formatDateTime(toDateTime64({term_sql}, {CLICKHOUSE_DATETIME_PRECISION}, 'UTC'), "
            f"'{CLICKHOUSE_DATETIME_TEXT_FORMAT}', 'UTC'), '\\\\.000000$', '')"
        )
        return f"concat({text_sql}, '+00:00')" if function.is_aware else text_sql
