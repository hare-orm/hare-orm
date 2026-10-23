from __future__ import annotations

from hare.dialects.clickhouse.constants import CLICKHOUSE_DATETIME_PRECISION
from hare.dialects.clickhouse.renderers.constants import (
    CLICKHOUSE_DECIMAL_CAST_TEMPLATE,
    CLICKHOUSE_NUMERIC_CAST_TYPE,
    CLICKHOUSE_TEXT_MOMENT_TEMPLATE,
    CLICKHOUSE_TEXT_WITHOUT_OFFSET_TEMPLATE,
    CLICKHOUSE_UTC_ZONE_SQL,
)
from hare.sql.enums import CastType
from hare.sql.functions.cast_to import CastTo
from hare.sql.terms.values.value_wrapper import ValueWrapper


class ClickhouseValueCasts:
    """A value cast to a number, a boolean or a moment as ClickHouse converts, refusing what its own
    conversions would silently turn into another value: an integer out of the target's range (wrapped),
    a Decimal past its digits, an impossible date (rolled over), bad text of a Nullable column (read as
    NULL); a NULL row stays NULL. A Decimal rounds half away from zero to its places, as a
    ``DecimalField`` rounds."""

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
        if source is not CastType.TEXT:
            if function.target is CastType.BOOLEAN:
                return term_sql if source is CastType.BOOLEAN else f"toBool({term_sql})"
            if function.target in {CastType.DATE, CastType.DATETIME}:
                if not function.is_aware and source is CastType.DATETIME and function.target is CastType.DATE:
                    # The date of the local wall clock the moment is the instant of.
                    term_sql = f"toTimeZone({term_sql}, {zone_sql})"
                elif not function.is_aware and source is CastType.DATE and function.target is CastType.DATETIME:
                    # The instant of the date's local midnight.
                    term_sql = f"toDateTime64({term_sql}, {CLICKHOUSE_DATETIME_PRECISION}, {zone_sql})"
                return f"CAST({term_sql} AS Nullable({column_type}))"
            return cls.get_number_sql(term_sql, source, function, column_type, keeps_null=True)
        text_sql = term_sql
        if function.target is CastType.BOOLEAN:
            return f"toBool({text_sql})"
        if function.target in {CastType.DATE, CastType.DATETIME}:
            # A naive timestamp reads its text's own clock, a date its text's own date.
            if not function.is_aware or function.target is CastType.DATE:
                text_sql = CLICKHOUSE_TEXT_WITHOUT_OFFSET_TEMPLATE.format(text_sql)
            # A date reads its text's own date; a naive moment its text's local wall clock.
            moment_zone_sql = CLICKHOUSE_UTC_ZONE_SQL if function.target is CastType.DATE else zone_sql
            moment_sql = CLICKHOUSE_TEXT_MOMENT_TEMPLATE.format(text_sql, moment_zone_sql)
            return f"CAST({moment_sql} AS Nullable({column_type}))"
        # A text converted into a Nullable type is NULL of bad input - a NULL's text is read as 0.
        number_sql = cls.get_number_sql(f"ifNull({text_sql}, '0')", source, function, column_type, keeps_null=False)
        return f"if(isNull({term_sql}), NULL, {number_sql})"

    @staticmethod
    def get_number_sql(
        term_sql: str, source: CastType, function: CastTo, column_type: str, *, keeps_null: bool
    ) -> str:
        """A number - an integer with the fraction cut off, a Decimal rounded half away from zero to the
        target's places; one out of the target's range is refused.

        Args:
            term_sql: The value's SQL.
            source: The type of the value.
            function: The cast.
            column_type: The ClickHouse type of the target.
            keeps_null: Whether the value may be NULL - else bad text is refused, not read as NULL.

        Returns:
            The SQL of the number.
        """
        if function.target is CastType.FLOAT:
            return f"toFloat64({term_sql})"
        if function.target is CastType.INTEGER:
            if source in {CastType.FLOAT, CastType.DECIMAL}:
                # accurateCast() refuses a fraction - ClickHouse's own conversion cuts it off.
                term_sql = f"trunc({term_sql})"
            return f"accurateCast({term_sql}, '{f'Nullable({column_type})' if keeps_null else column_type}')"
        max_digits, decimal_places = function.parameters
        if max_digits is None or decimal_places is None:
            return f"CAST({term_sql} AS Nullable({column_type}))"
        if source is CastType.INTEGER:
            term_sql = f"toDecimal256({term_sql}, 0)"
        elif source is CastType.FLOAT:
            # By the float's shortest text - toDecimal256() of the double itself scales it in floating
            # point, 2.5 to 2.4999...
            term_sql = CLICKHOUSE_NUMERIC_CAST_TYPE.format(f"toString({term_sql})")
        elif source is not CastType.DECIMAL:
            term_sql = CLICKHOUSE_NUMERIC_CAST_TYPE.format(term_sql)
        return CLICKHOUSE_DECIMAL_CAST_TEMPLATE.format(
            term_sql, decimal_places, 10 ** (max_digits - decimal_places), f"Nullable({column_type})"
        )
