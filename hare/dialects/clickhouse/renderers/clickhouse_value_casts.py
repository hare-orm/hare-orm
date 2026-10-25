from __future__ import annotations

from hare.dialects.clickhouse.constants import CLICKHOUSE_DATETIME_PRECISION
from hare.dialects.clickhouse.renderers.constants import (
    CLICKHOUSE_TEXT_WITHOUT_OFFSET_TEMPLATE,
    CLICKHOUSE_UTC_ZONE_SQL,
)
from hare.sql.enums import CastType
from hare.sql.functions.cast_to import CastTo
from hare.sql.terms.values.value_wrapper import ValueWrapper


class ClickhouseValueCasts:
    """A value cast to a number, a boolean or a moment by ClickHouse's own conversions - a text read as a
    moment by ``parseDateTime64BestEffort()``; a NULL stays NULL."""

    @staticmethod
    def get_source(function: CastTo) -> CastType:
        """The type of the value - a text literal of no known type is text."""
        argument = function.args[0]
        if (
            function.source is CastType.UNKNOWN
            and isinstance(argument, ValueWrapper)
            and isinstance(argument.value, str)
        ):
            return CastType.TEXT
        return function.source

    @classmethod
    def get_cast_sql(cls, term_sql: str, function: CastTo, column_type: str, zone_sql: str) -> str:
        """The value converted to ``column_type`` - a number, a boolean, a date or a datetime.

        Args:
            term_sql: The value's SQL.
            function: The cast.
            column_type: The ClickHouse type of the target.
            zone_sql: The zone a moment is read in - the local one for a naive moment.

        Returns:
            The SQL of the converted value, NULL for NULL.
        """
        if column_type.startswith("Nullable("):
            column_type = column_type.removeprefix("Nullable(").removesuffix(")")
        source = cls.get_source(function)
        if function.target is CastType.BOOLEAN:
            return term_sql if source is CastType.BOOLEAN else f"toBool({term_sql})"
        if function.target in {CastType.DATE, CastType.DATETIME}:
            if source is CastType.TEXT:
                # A naive timestamp reads its text's own clock, a date its text's own date.
                if not function.is_aware or function.target is CastType.DATE:
                    term_sql = CLICKHOUSE_TEXT_WITHOUT_OFFSET_TEMPLATE.format(term_sql)
                # A date reads its text's own date; a naive moment its text's local wall clock.
                moment_zone_sql = CLICKHOUSE_UTC_ZONE_SQL if function.target is CastType.DATE else zone_sql
                term_sql = f"parseDateTime64BestEffort({term_sql}, {CLICKHOUSE_DATETIME_PRECISION}, {moment_zone_sql})"
            elif not function.is_aware and source is CastType.DATETIME and function.target is CastType.DATE:
                # The date of the local wall clock the moment is the instant of.
                term_sql = f"toTimeZone({term_sql}, {zone_sql})"
            elif not function.is_aware and source is CastType.DATE and function.target is CastType.DATETIME:
                # The instant of the date's local midnight.
                term_sql = f"toDateTime64({term_sql}, {CLICKHOUSE_DATETIME_PRECISION}, {zone_sql})"
        return f"CAST({term_sql} AS Nullable({column_type}))"
