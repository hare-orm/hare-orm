from __future__ import annotations

from typing import TYPE_CHECKING

from hare.sql import functions
from hare.sql.constants import MICROSECONDS_PER_DAY, MICROSECONDS_PER_HOUR, MICROSECONDS_PER_SECOND
from hare.sql.enums import DatePart, DateTruncSource, TruncType
from hare.sql.sql_context import SqlContext
from hare.sql.terms import functions as term_functions
from hare.sql.terms.values.value_wrapper import ValueWrapper
from hare.sql.types.sql_types import SqlTypes
from hare.time import Timezone

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.renderers.term_renderers import TermRenderers


class PostgresqlTemporalRenderers:
    """How PostgreSQL writes dates and times: the current moment and date, a part extracted, a value
    truncated, cast, shifted by an interval or subtracted, a date compared with a timestamp, and a
    moment written as text."""

    @classmethod
    def register(cls, renderers: TermRenderers) -> None:
        """Registers the temporal renderers on PostgreSQL's renderers.

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
    def get_local_zone_sql(sql_context: SqlContext) -> str:
        """The machine's local zone as a literal - PostgreSQL holds a naive timestamp as an instant
        in it."""
        return ValueWrapper(Timezone.get_local_zone_name(), allow_parametrize=False).get_sql(sql_context)

    @staticmethod
    def get_extract_source_sql(function: functions.Extract, sql_context: SqlContext) -> str:
        """``FROM field``, in the extraction's zone. A naive ``DatetimeField`` is read in the machine's
        local zone - PostgreSQL holds its value as an instant there, while a bare ``EXTRACT`` reads it
        in the session's zone."""
        field_sql = function.field.get_sql(sql_context)
        zone_name = function.zone_name
        if zone_name is None and function.use_local_zone_when_naive:
            zone_name = Timezone.get_local_zone_name()
        if zone_name is None:
            return f"FROM {field_sql}"
        # The cast reads a `timestamp` column as the UTC wall clock it holds - a no-op for TIMESTAMPTZ.
        zone_sql = ValueWrapper(zone_name, allow_parametrize=not function.zone_as_literal).get_sql(sql_context)
        return f"FROM (CAST({field_sql} AS TIMESTAMPTZ) AT TIME ZONE {zone_sql})"

    @classmethod
    def render_extract(cls, function: functions.Extract, sql_context: SqlContext) -> str:
        """``EXTRACT``, with each part normalized to Python's ``datetime`` semantics - PostgreSQL's
        SECOND carries the fraction, MICROSECOND folds in the seconds, DOW counts from 0."""
        date_part_sql = function.get_arg_sql(function.args[0], sql_context)
        extract_sql = f"EXTRACT({date_part_sql} {cls.get_extract_source_sql(function, sql_context)})"
        if function.date_part == DatePart.SECOND:
            return f"FLOOR({extract_sql})::integer"
        if function.date_part == DatePart.MICROSECOND:
            return f"MOD({extract_sql}::integer, 1000000)"
        if function.date_part == DatePart.WEEK_DAY:
            # week_day is 1 (Sunday) to 7, as in Django.
            return f"({extract_sql}::integer + 1)"
        if function.as_integer:
            return f"CAST({extract_sql} AS INTEGER)"
        return extract_sql

    @staticmethod
    def render_date_trunc(function: functions.DateTrunc, sql_context: SqlContext) -> str:
        """``DATE_TRUNC``; a time of day is truncated as its interval since midnight, which keeps a
        ``TIMETZ`` offset."""
        field_sql = function.field.get_sql(sql_context)
        unit_sql = ValueWrapper(function.trunc_type.value, allow_parametrize=False).get_sql(sql_context)
        if function.source == DateTruncSource.DATETIME:
            # A literal, not a parameter, so the selected expression and its GROUP BY stay
            # identical - the zone is a validated IANA name or the system's own zone name.
            zone_sql = ValueWrapper(function.zone_name or "UTC", allow_parametrize=False).get_sql(sql_context)
            local_sql = f"(CAST({field_sql} AS TIMESTAMPTZ) AT TIME ZONE {zone_sql})"
            if function.trunc_type in {TruncType.DATE, TruncType.TIME}:
                return f"CAST({local_sql} AS {function.trunc_type.value.upper()})"
            return f"(DATE_TRUNC({unit_sql}, {local_sql}) AT TIME ZONE {zone_sql})"
        if function.source == DateTruncSource.DATE:
            if function.trunc_type == TruncType.DATE:
                return field_sql
            return f"CAST(DATE_TRUNC({unit_sql}, CAST({field_sql} AS TIMESTAMP)) AS DATE)"
        if function.trunc_type == TruncType.TIME:
            return field_sql
        since_midnight_sql = f"(CAST({field_sql} AS TIME) - TIME '00:00:00')"
        return f"({field_sql} - ({since_midnight_sql} - DATE_TRUNC({unit_sql}, {since_midnight_sql})))"

    @staticmethod
    def render_datetime_cast(function: functions.DatetimeCast, sql_context: SqlContext) -> str:
        """The value cast at its zone."""
        field_sql = function.field.get_sql(sql_context)
        zone_name = function.zone_name
        if zone_name is None and function.use_local_zone_when_naive:
            zone_name = Timezone.get_local_zone_name()
        if zone_name is not None:
            field_sql = (
                f"(CAST({field_sql} AS TIMESTAMPTZ) AT TIME ZONE {ValueWrapper(zone_name).get_sql(sql_context)})"
            )
        return f"CAST({field_sql} AS {function.target.value})"

    @staticmethod
    def render_temporal_shift(function: functions.TemporalShift, sql_context: SqlContext) -> str:
        """``interval`` arithmetic of whole hours plus a microsecond remainder - exact for any
        ``bigint``, and never a calendar day, which would shift with the session zone's DST; a date
        result is truncated back to a date."""
        base_term, microseconds_term = function.args
        base_type = SqlTypes.DATE if function.is_date else SqlTypes.TIMESTAMPTZ
        operator = "-" if function.is_subtraction else "+"
        microseconds_sql = f"CAST({function.get_arg_sql(microseconds_term, sql_context)} AS {SqlTypes.BIGINT})"
        interval_sql = (
            f"({microseconds_sql}/{MICROSECONDS_PER_HOUR}*INTERVAL '1 hour'"
            f"+{microseconds_sql}%{MICROSECONDS_PER_HOUR}*INTERVAL '1 microsecond')"
        )
        shifted_sql = f"CAST({function.get_arg_sql(base_term, sql_context)} AS {base_type}){operator}{interval_sql}"
        if function.is_date:
            return f"CAST({shifted_sql} AS {SqlTypes.DATE})"
        return f"({shifted_sql})"

    @staticmethod
    def render_temporal_difference(function: functions.TemporalDifference, sql_context: SqlContext) -> str:
        """``EXTRACT(EPOCH ...)`` of datetimes (exact numeric on PostgreSQL 14+), the day difference
        of dates."""
        left_term, right_term = function.args
        left_sql = function.get_arg_sql(left_term, sql_context)
        right_sql = function.get_arg_sql(right_term, sql_context)
        if function.is_date:
            return (
                f"((CAST({left_sql} AS {SqlTypes.DATE})-CAST({right_sql} AS {SqlTypes.DATE}))*{MICROSECONDS_PER_DAY})"
            )
        interval_sql = f"(CAST({left_sql} AS {SqlTypes.TIMESTAMPTZ})-CAST({right_sql} AS {SqlTypes.TIMESTAMPTZ}))"
        epoch_sql = f"CAST(EXTRACT(EPOCH FROM {interval_sql}) AS {SqlTypes.NUMERIC})"
        return f"CAST(ROUND({epoch_sql}*{MICROSECONDS_PER_SECOND}) AS {SqlTypes.BIGINT})"

    @classmethod
    def render_date_as_timestamp(cls, function: functions.DateAsTimestamp, sql_context: SqlContext) -> str:
        term_sql = function.get_arg_sql(function.args[0], sql_context)
        if function.zone_name is None:
            zone_sql = cls.get_local_zone_sql(sql_context)
        else:
            zone_sql = ValueWrapper(function.zone_name, allow_parametrize=False).get_sql(sql_context)
        return f"(CAST({term_sql} AS TIMESTAMP) AT TIME ZONE {zone_sql})"

    @staticmethod
    def render_interval(interval: term_functions.Interval, sql_context: SqlContext) -> str:
        expression, unit = interval.get_expression_and_unit()
        return f"INTERVAL '{expression} {unit}'"

    @classmethod
    def render_datetime_as_text(cls, function: functions.DatetimeAsText, sql_context: SqlContext) -> str:
        """The text SQLite stores a timestamp in - an aware value in UTC, a naive one as its wall clock."""
        term_sql = function.get_arg_sql(function.args[0], sql_context)
        if not function.is_aware:
            local_sql = f"(CAST({term_sql} AS TIMESTAMPTZ) AT TIME ZONE {cls.get_local_zone_sql(sql_context)})"
            return f"REPLACE(TO_CHAR({local_sql},'YYYY-MM-DD HH24:MI:SS.US'),'.000000','')"
        return f"(REPLACE(TO_CHAR({term_sql} AT TIME ZONE 'UTC','YYYY-MM-DD HH24:MI:SS.US'),'.000000','')||'+00:00')"
