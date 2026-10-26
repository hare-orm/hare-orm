from __future__ import annotations

from typing import TYPE_CHECKING

from hare.dialects.clickhouse.constants import CLICKHOUSE_DATETIME_PRECISION
from hare.dialects.clickhouse.renderers.constants import (
    CLICKHOUSE_DATE_PART_FUNCTIONS,
    CLICKHOUSE_NOW_SQL,
    CLICKHOUSE_REMAINDER_TRUNC_MICROSECONDS,
    CLICKHOUSE_REMAINDER_TRUNC_TEMPLATE,
    CLICKHOUSE_TIME_OFFSET_PATTERN,
    CLICKHOUSE_TIME_TEXT_FORMAT,
    CLICKHOUSE_UTC_ZONE_SQL,
    CLICKHOUSE_WHOLE_SECOND_FRACTION_PATTERN,
)
from hare.fields.db_defaults.now import Now as NowDefault
from hare.fields.enums import NowValueType
from hare.sql import functions
from hare.sql.constants import MICROSECONDS_PER_DAY
from hare.sql.enums import DatetimeCastTarget, DateTruncSource, TruncType
from hare.sql.sql_context import SqlContext
from hare.sql.terms import functions as term_functions
from hare.time import Timezone

if TYPE_CHECKING:
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
        renderers.register(functions.Now, cls.render_now)
        renderers.register(NowDefault, cls.render_now_default)

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

    @staticmethod
    def get_function_zone_name(function: functions.Extract | functions.DatetimeCast) -> str | None:
        """The zone a moment's part or date is read in - the function's own, else the machine's local
        zone for a naive ``DatetimeField`` (``use_timezone=False``): the column holds the instant of its
        local wall clock.

        Args:
            function: The function.

        Returns:
            The zone, None for UTC.
        """
        if function.zone_name is None and function.use_local_zone_when_naive:
            return Timezone.get_local_zone_name()
        return function.zone_name

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

    @staticmethod
    def render_now(function: functions.Now, sql_context: SqlContext) -> str:
        """The current moment, as a moment is stored - ClickHouse has no ``CURRENT_TIMESTAMP``."""
        return CLICKHOUSE_NOW_SQL

    @classmethod
    def render_now_default(cls, default: NowDefault, sql_context: SqlContext) -> str:
        """The current moment as a column default and as ``Now()`` in a query - a moment of UTC; the
        date or time of day of a ``DateField``/``TimeField`` at the configured zone.

        Args:
            default: The default.
            sql_context: The rendering context.

        Returns:
            The SQL.
        """
        if default.value_type == NowValueType.DATETIME:
            return CLICKHOUSE_NOW_SQL
        zone_name = Timezone.name() if Timezone.get_use_timezone() else Timezone.get_local_zone_name()
        zone_sql = cls.get_zone_sql(zone_name, sql_context)
        if default.value_type == NowValueType.DATE:
            return f"toDate32({CLICKHOUSE_NOW_SQL}, {zone_sql})"
        return cls.get_time_text_sql(CLICKHOUSE_NOW_SQL, zone_sql)

    @classmethod
    def render_extract(cls, function: functions.Extract, sql_context: SqlContext) -> str:
        """The part, in Python's ``datetime`` meaning; a time of day is read from its text."""
        field_sql = function.field.get_sql(sql_context)
        if function.is_time_of_day:
            field_sql = cls.get_time_moment_sql(field_sql)
        source_sql = cls.get_zoned_sql(field_sql, cls.get_function_zone_name(function), sql_context)
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
            remainder_unit_microseconds = CLICKHOUSE_REMAINDER_TRUNC_MICROSECONDS.get(function.trunc_type)
            if remainder_unit_microseconds is not None:
                moment_sql = f"toDateTime64({field_sql}, {CLICKHOUSE_DATETIME_PRECISION}, 'UTC')"
                return CLICKHOUSE_REMAINDER_TRUNC_TEMPLATE.format(moment_sql, zone_sql, remainder_unit_microseconds)
            # The moment is truncated at the zone's wall clock - its midnight is a moment of the zone
            # too, not of UTC. dateTrunc rounds a fraction of a moment before 1970 toward 1970 - whole
            # seconds first.
            truncated_sql = f"dateTrunc({unit_sql}, toTimeZone(toStartOfSecond({field_sql}), {zone_sql}))"
            zoned_moment_sql = f"toDateTime64({truncated_sql}, {CLICKHOUSE_DATETIME_PRECISION}, {zone_sql})"
            return f"toDateTime64({zoned_moment_sql}, {CLICKHOUSE_DATETIME_PRECISION}, 'UTC')"
        if function.source == DateTruncSource.DATE:
            if function.trunc_type == TruncType.DATE:
                return field_sql
            return f"toDate32(dateTrunc({unit_sql}, toDateTime64({field_sql}, 0, 'UTC')))"
        if function.trunc_type == TruncType.TIME:
            return field_sql
        return cls.get_time_text_sql(f"dateTrunc({unit_sql}, {cls.get_time_moment_sql(field_sql)})", "'UTC'")

    @staticmethod
    def get_time_moment_sql(time_text_sql: str) -> str:
        """A time of day stored as ISO text, read as that wall-clock time of 1970-01-01 in UTC - an
        offset the text ends with is left out, as Python reads the hour of an aware time.

        Args:
            time_text_sql: The text.

        Returns:
            The SQL.
        """
        wall_clock_sql = f"replaceRegexpOne({time_text_sql}, '{CLICKHOUSE_TIME_OFFSET_PATTERN}', '')"
        moment_text_sql = f"concat('1970-01-01 ', {wall_clock_sql})"
        return f"parseDateTime64BestEffort({moment_text_sql}, {CLICKHOUSE_DATETIME_PRECISION}, 'UTC')"

    @classmethod
    def render_datetime_cast(cls, function: functions.DatetimeCast, sql_context: SqlContext) -> str:
        """The date or the time of day of a moment, at its zone."""
        zone_sql = cls.get_zone_sql(cls.get_function_zone_name(function) or "UTC", sql_context)
        field_sql = function.field.get_sql(sql_context)
        if function.target == DatetimeCastTarget.DATE:
            return f"toDate32({field_sql}, {zone_sql})"
        return cls.get_time_text_sql(field_sql, zone_sql)

    @staticmethod
    def render_temporal_shift(function: functions.TemporalShift, sql_context: SqlContext) -> str:
        """The moment or the date shifted by whole microseconds - never a calendar day - by ClickHouse's
        own ``addMicroseconds()``."""
        base_term, microseconds_term = function.args
        base_sql = function.get_arg_sql(base_term, sql_context)
        microseconds_sql = function.get_arg_sql(microseconds_term, sql_context)
        moment_sql = f"toDateTime64({base_sql}, {CLICKHOUSE_DATETIME_PRECISION}, 'UTC')"
        shift_sql = f"toInt64({microseconds_sql})"
        if function.is_subtraction:
            shift_sql = f"negate({shift_sql})"
        shifted_sql = f"addMicroseconds({moment_sql}, {shift_sql})"
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
    def get_moment_zone_sql(is_aware: bool, sql_context: SqlContext) -> str:
        """The zone a moment is read in - UTC for an aware one; the machine's local zone for a naive one
        (``use_timezone=False``), whose column holds the instant of its local wall clock.

        Args:
            is_aware: Whether the moment is aware.
            sql_context: The context.

        Returns:
            The zone's literal.
        """
        if is_aware:
            return CLICKHOUSE_UTC_ZONE_SQL
        return sql_context.dialect.literals.get_string_literal_sql(Timezone.get_local_zone_name())

    @classmethod
    def render_datetime_as_text(cls, function: functions.DatetimeAsText, sql_context: SqlContext) -> str:
        """ClickHouse's text of the moment - a naive one, held as the instant of its wall clock in the
        machine's local zone, at that zone."""
        term_sql = function.get_arg_sql(function.args[0], sql_context)
        return f"toString({term_sql}, {cls.get_moment_zone_sql(function.is_aware, sql_context)})"
