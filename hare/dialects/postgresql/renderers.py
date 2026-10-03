from __future__ import annotations

from hare.dialects.base.renderers.dialect_renderers import DialectRenderers
from hare.dialects.base.renderers.term_renderers import TermRenderers
from hare.dialects.postgresql.constants import POSTGRESQL_DIGEST_FUNCTIONS, POSTGRESQL_NUMERIC_ONLY_MATH_FUNCTIONS
from hare.dialects.postgresql.defaults import PostgresqlDefaults
from hare.exceptions import UnSupportedError
from hare.sql import functions
from hare.sql.constants import (
    MICROSECONDS_PER_DAY,
    MICROSECONDS_PER_HOUR,
    MICROSECONDS_PER_SECOND,
)
from hare.sql.context import SqlContext
from hare.sql.enums import CastType, DatePart, DateTruncSource, JsonValueType, TruncType
from hare.sql.sql_types import SqlTypes
from hare.sql.terms import criteria, functions as term_functions
from hare.sql.terms.base.value_wrapper import ValueWrapper
from hare.utils import Timezone


class PostgresqlRenderers(DialectRenderers):
    """How PostgreSQL renders the terms whose SQL differs between dialects."""

    @classmethod
    def add_own_renderers(cls, renderers: TermRenderers) -> None:
        PostgresqlDefaults.register(renderers)
        renderers.register(functions.Round, cls.render_round)

    @staticmethod
    def render_round(function: functions.Round, ctx: SqlContext) -> str:
        """``ROUND(x, places)`` - PostgreSQL has it for an exact decimal ``x`` only."""
        term_sql, places_sql = (function.get_arg_sql(argument, ctx) for argument in function.args)
        return f"ROUND(CAST({term_sql} AS NUMERIC),{places_sql})"

    @staticmethod
    def get_local_zone_sql(ctx: SqlContext) -> str:
        """The machine's local zone as a literal - PostgreSQL holds a naive timestamp as an instant
        in it."""
        return ValueWrapper(Timezone.get_local_zone_name(), allow_parametrize=False).get_sql(ctx)

    @staticmethod
    def render_boolean_as_text(function: functions.BooleanAsText, ctx: SqlContext) -> str:
        return f"CAST({function.get_arg_sql(function.args[0], ctx)} AS TEXT)"

    @staticmethod
    def render_float_as_text(function: functions.FloatAsText, ctx: SqlContext) -> str:
        return f"CAST(CAST({function.get_arg_sql(function.args[0], ctx)} AS DOUBLE PRECISION) AS TEXT)"

    @staticmethod
    def render_decimal_as_text(function: functions.DecimalAsText, ctx: SqlContext) -> str:
        term_sql = function.get_arg_sql(function.args[0], ctx)
        return f"CAST(ROUND(CAST({term_sql} AS NUMERIC),{function.scale}) AS TEXT)"

    @classmethod
    def render_datetime_as_text(cls, function: functions.DatetimeAsText, ctx: SqlContext) -> str:
        """The text SQLite stores a timestamp in - an aware value in UTC, a naive one as its wall clock."""
        term_sql = function.get_arg_sql(function.args[0], ctx)
        if not function.is_aware:
            local_sql = f"(CAST({term_sql} AS TIMESTAMPTZ) AT TIME ZONE {cls.get_local_zone_sql(ctx)})"
            return f"REPLACE(TO_CHAR({local_sql},'YYYY-MM-DD HH24:MI:SS.US'),'.000000','')"
        return f"(REPLACE(TO_CHAR({term_sql} AT TIME ZONE 'UTC','YYYY-MM-DD HH24:MI:SS.US'),'.000000','')||'+00:00')"

    @staticmethod
    def get_extract_source_sql(function: functions.Extract, ctx: SqlContext) -> str:
        """``FROM field``, in the extraction's zone. A naive ``DatetimeField`` is read in the machine's
        local zone - PostgreSQL holds its value as an instant there, while a bare ``EXTRACT`` reads it
        in the session's zone."""
        field_sql = function.field.get_sql(ctx)
        zone_name = function.zone_name
        if zone_name is None and function.use_local_zone_when_naive:
            zone_name = Timezone.get_local_zone_name()
        if zone_name is None:
            return f"FROM {field_sql}"
        # The cast reads a `timestamp` column as the UTC wall clock it holds - a no-op for TIMESTAMPTZ.
        zone_sql = ValueWrapper(zone_name, allow_parametrize=not function.zone_as_literal).get_sql(ctx)
        return f"FROM (CAST({field_sql} AS TIMESTAMPTZ) AT TIME ZONE {zone_sql})"

    @classmethod
    def render_extract(cls, function: functions.Extract, ctx: SqlContext) -> str:
        """``EXTRACT``, with each part normalized to Python's ``datetime`` semantics - PostgreSQL's
        SECOND carries the fraction, MICROSECOND folds in the seconds, DOW counts from 0."""
        date_part_sql = function.get_arg_sql(function.args[0], ctx)
        extract_sql = f"EXTRACT({date_part_sql} {cls.get_extract_source_sql(function, ctx)})"
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
    def render_math_function(function: functions.MathFunction, ctx: SqlContext) -> str:
        args_sql = [function.get_arg_sql(arg, ctx) for arg in function.args]
        if function.name in POSTGRESQL_NUMERIC_ONLY_MATH_FUNCTIONS:
            # Only a numeric variant exists (MOD of fractions, LOG(base, x)).
            args_sql = [f"CAST({arg_sql} AS NUMERIC)" for arg_sql in args_sql]
        return f"{function.name}({','.join(args_sql)})"

    @classmethod
    def render_cast_to(cls, function: functions.CastTo, ctx: SqlContext) -> str:
        """The cast - a naive timestamp read or made in the machine's local zone."""
        term_sql = function.get_arg_sql(function.args[0], ctx)
        if not function.is_aware and (function.source == CastType.DATETIME) != (function.target == CastType.DATETIME):
            zone_sql = cls.get_local_zone_sql(ctx)
            if function.target == CastType.DATETIME:
                return f"(CAST({term_sql} AS TIMESTAMP) AT TIME ZONE {zone_sql})"
            term_sql = f"(CAST({term_sql} AS TIMESTAMPTZ) AT TIME ZONE {zone_sql})"
        # A time of day is cast without its offset.
        target_type = (
            "TIME" if function.target == CastType.TIME else function.target_field.get_column_type(ctx.dialect)
        )
        return f"CAST({term_sql} AS {target_type})"

    @staticmethod
    def render_text_function(function: functions.TextFunction, ctx: SqlContext) -> str:
        """The function with its first argument read as text."""
        args_sql = [function.get_arg_sql(arg, ctx) for arg in function.args]
        # CHR takes an integer code point - there's no bigint variant.
        args_sql[0] = f"CAST({args_sql[0]} AS {'INTEGER' if function.name == 'CHR' else 'TEXT'})"
        if function.name in POSTGRESQL_DIGEST_FUNCTIONS:
            return f"ENCODE({function.name}(CONVERT_TO({args_sql[0]}, 'UTF8')), 'hex')"
        if function.name == "SHA1":
            return f"ENCODE(DIGEST(CONVERT_TO({args_sql[0]}, 'UTF8'), 'sha1'), 'hex')"
        return f"{function.name}({','.join(args_sql)})"

    @staticmethod
    def render_date_trunc(function: functions.DateTrunc, ctx: SqlContext) -> str:
        """``DATE_TRUNC``; a time of day is truncated as its interval since midnight, which keeps a
        ``TIMETZ`` offset."""
        field_sql = function.field.get_sql(ctx)
        unit_sql = ValueWrapper(function.trunc_type.value, allow_parametrize=False).get_sql(ctx)
        if function.source == DateTruncSource.DATETIME:
            # A literal, not a parameter, so the selected expression and its GROUP BY stay
            # identical - the zone is a validated IANA name or the system's own zone name.
            zone_sql = ValueWrapper(function.zone_name or "UTC", allow_parametrize=False).get_sql(ctx)
            local_sql = f"(CAST({field_sql} AS TIMESTAMPTZ) AT TIME ZONE {zone_sql})"
            if function.trunc_type in (TruncType.DATE, TruncType.TIME):
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
    def render_datetime_cast(function: functions.DatetimeCast, ctx: SqlContext) -> str:
        """The value cast at its zone."""
        field_sql = function.field.get_sql(ctx)
        zone_name = function.zone_name
        if zone_name is None and function.use_local_zone_when_naive:
            zone_name = Timezone.get_local_zone_name()
        if zone_name is not None:
            field_sql = f"(CAST({field_sql} AS TIMESTAMPTZ) AT TIME ZONE {ValueWrapper(zone_name).get_sql(ctx)})"
        return f"CAST({field_sql} AS {function.target.value})"

    @staticmethod
    def render_temporal_shift(function: functions.TemporalShift, ctx: SqlContext) -> str:
        """``interval`` arithmetic of whole hours plus a microsecond remainder - exact for any
        ``bigint``, and never a calendar day, which would shift with the session zone's DST; a date
        result is truncated back to a date."""
        base_term, microseconds_term = function.args
        base_type = SqlTypes.DATE if function.is_date else SqlTypes.TIMESTAMPTZ
        operator = "-" if function.is_subtraction else "+"
        microseconds_sql = f"CAST({function.get_arg_sql(microseconds_term, ctx)} AS {SqlTypes.BIGINT})"
        interval_sql = (
            f"({microseconds_sql}/{MICROSECONDS_PER_HOUR}*INTERVAL '1 hour'"
            f"+{microseconds_sql}%{MICROSECONDS_PER_HOUR}*INTERVAL '1 microsecond')"
        )
        shifted_sql = f"CAST({function.get_arg_sql(base_term, ctx)} AS {base_type}){operator}{interval_sql}"
        if function.is_date:
            return f"CAST({shifted_sql} AS {SqlTypes.DATE})"
        return f"({shifted_sql})"

    @staticmethod
    def render_temporal_difference(function: functions.TemporalDifference, ctx: SqlContext) -> str:
        """``EXTRACT(EPOCH ...)`` of datetimes (exact numeric on PostgreSQL 14+), the day difference
        of dates."""
        left_term, right_term = function.args
        left_sql = function.get_arg_sql(left_term, ctx)
        right_sql = function.get_arg_sql(right_term, ctx)
        if function.is_date:
            return (
                f"((CAST({left_sql} AS {SqlTypes.DATE})-CAST({right_sql} AS {SqlTypes.DATE}))*{MICROSECONDS_PER_DAY})"
            )
        interval_sql = f"(CAST({left_sql} AS {SqlTypes.TIMESTAMPTZ})-CAST({right_sql} AS {SqlTypes.TIMESTAMPTZ}))"
        epoch_sql = f"CAST(EXTRACT(EPOCH FROM {interval_sql}) AS {SqlTypes.NUMERIC})"
        return f"CAST(ROUND({epoch_sql}*{MICROSECONDS_PER_SECOND}) AS {SqlTypes.BIGINT})"

    @staticmethod
    def render_json_object(function: functions.JsonObject, ctx: SqlContext) -> str:
        return f"JSONB_BUILD_OBJECT({','.join(function.get_arg_sql(argument, ctx) for argument in function.args)})"

    @classmethod
    def render_json_value(cls, function: functions.JsonValue, ctx: SqlContext) -> str:
        """The value as it is - a naive timestamp as its wall clock."""
        term_sql = function.get_arg_sql(function.args[0], ctx)
        if function.value_type != JsonValueType.DATETIME or function.is_aware:
            return term_sql
        return f"(CAST({term_sql} AS TIMESTAMPTZ) AT TIME ZONE {cls.get_local_zone_sql(ctx)})"

    @classmethod
    def render_json_comparand(cls, function: functions.JsonComparand, ctx: SqlContext) -> str:
        return f"to_jsonb({cls.render_json_value(function, ctx)})"

    @classmethod
    def render_date_as_timestamp(cls, function: functions.DateAsTimestamp, ctx: SqlContext) -> str:
        term_sql = function.get_arg_sql(function.args[0], ctx)
        if function.zone_name is None:
            zone_sql = cls.get_local_zone_sql(ctx)
        else:
            zone_sql = ValueWrapper(function.zone_name, allow_parametrize=False).get_sql(ctx)
        return f"(CAST({term_sql} AS TIMESTAMP) AT TIME ZONE {zone_sql})"

    @staticmethod
    def render_json_sort_key(function: functions.JsonSortKey, ctx: SqlContext) -> str:
        """The value itself - jsonb orders its values."""
        return function.get_arg_sql(function.args[0], ctx)

    @staticmethod
    def render_interval(interval: term_functions.Interval, ctx: SqlContext) -> str:
        expression, unit = interval.get_expression_and_unit()
        return f"INTERVAL '{expression} {unit}'"

    @staticmethod
    def render_float_mod(function: term_functions.FloatMod, ctx: SqlContext) -> str:
        """``x - y * TRUNC(x / y)`` - there's no ``MOD()`` for double precision."""
        left_sql, right_sql = (f"CAST({function.get_arg_sql(arg, ctx)} AS DOUBLE PRECISION)" for arg in function.args)
        return f"({left_sql}-{right_sql}*TRUNC({left_sql}/{right_sql}))"

    @staticmethod
    def render_json_attribute(criterion: criteria.JSONAttributeCriterion, ctx: SqlContext) -> str:
        """``data->'user'->>'age'``; a digit segment ``#>``/``#>>`` with a one-element text path,
        which reads an object's key or an array's index, whichever the value there is."""
        sql = criterion.json_column.get_sql(ctx)
        for position, part in enumerate(criterion.path):
            is_last = position == len(criterion.path) - 1
            if isinstance(part, int):
                sql += f"{'#>>' if is_last and criterion.as_text else '#>'}'{{{part}}}'"
            else:
                operator = "->>" if is_last and criterion.as_text else "->"
                # A literal, not a parameter, so the expression matches an index on the same path.
                sql += f"{operator}{ctx.dialect.get_string_literal_sql(part)}"
        return sql

    @staticmethod
    def render_json_type(criterion: criteria.JSONTypeCriterion, ctx: SqlContext) -> str:
        raise UnSupportedError("The JSON type name of a path is SQLite's json_type() - PostgreSQL has no such text")
