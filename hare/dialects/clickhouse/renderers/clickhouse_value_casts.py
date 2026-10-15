from __future__ import annotations

from hare.dialects.clickhouse.renderers.constants import (
    CLICKHOUSE_BOOLEAN_CAST_INTEGER_BITS,
    CLICKHOUSE_CAST_SOURCES,
    CLICKHOUSE_CAST_TYPE_NAMES,
    CLICKHOUSE_DECIMAL_CAST_TEMPLATE,
    CLICKHOUSE_FLOAT_DECIMAL_TEMPLATE,
    CLICKHOUSE_INTEGER_TYPE_NAMES,
    CLICKHOUSE_NUMERIC_CAST_TYPE,
    CLICKHOUSE_TEXT_BOOLEAN_TEMPLATE,
    CLICKHOUSE_TEXT_MOMENT_TEMPLATE,
    CLICKHOUSE_TEXT_WITHOUT_OFFSET_TEMPLATE,
    CLICKHOUSE_TRIMMED_TEXT_TEMPLATE,
)
from hare.sql.enums import CastType
from hare.sql.functions.cast_to import CastTo
from hare.sql.terms.values.value_wrapper import ValueWrapper


class ClickhouseValueCasts:
    """A value cast to a number, a boolean or a moment with PostgreSQL's rules - ClickHouse's own
    conversions truncate a fraction, wrap an integer out of range, keep the digits a Decimal's
    precision doesn't hold, roll an impossible date over, read bad text of a Nullable column as NULL
    and fail on a NULL row."""

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
    def is_refused(cls, function: CastTo) -> bool:
        """Whether PostgreSQL has no cast from the value's type to the target's."""
        if function.target not in CLICKHOUSE_CAST_SOURCES:
            return False
        source = cls.get_source(function)
        if source is CastType.BOOLEAN and function.target is CastType.INTEGER:
            return function.parameters[0] != CLICKHOUSE_BOOLEAN_CAST_INTEGER_BITS
        return source not in CLICKHOUSE_CAST_SOURCES[function.target]

    @classmethod
    def get_refused_sql(cls, term_sql: str, function: CastTo) -> str:
        """PostgreSQL's error of a cast it has none of, for a value that isn't NULL."""
        target_name = CLICKHOUSE_CAST_TYPE_NAMES[function.target]
        if function.target is CastType.INTEGER:
            target_name = CLICKHOUSE_INTEGER_TYPE_NAMES.get(function.parameters[0] or 0, target_name)
        message = f"cannot cast type {CLICKHOUSE_CAST_TYPE_NAMES[cls.get_source(function)]} to {target_name}"
        return f"if(isNull({term_sql}), NULL, throwIf(1, '{message}'))"

    @classmethod
    def get_cast_sql(cls, term_sql: str, function: CastTo, column_type: str) -> str:
        """The value converted to ``column_type`` - a number, a boolean, a date or a datetime.

        Args:
            term_sql: The value's SQL.
            function: The cast.
            column_type: The ClickHouse type of the target.

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
                return f"CAST({term_sql} AS Nullable({column_type}))"
            return cls.get_number_sql(term_sql, source, function, column_type, keeps_null=True)
        trimmed_sql = CLICKHOUSE_TRIMMED_TEXT_TEMPLATE.format(term_sql)
        if function.target is CastType.BOOLEAN:
            return CLICKHOUSE_TEXT_BOOLEAN_TEMPLATE.format(trimmed_sql)
        if function.target in {CastType.DATE, CastType.DATETIME}:
            # A naive timestamp reads its text's own clock, a date its text's own date.
            if not function.is_aware or function.target is CastType.DATE:
                trimmed_sql = CLICKHOUSE_TEXT_WITHOUT_OFFSET_TEMPLATE.format(trimmed_sql)
            return f"CAST({CLICKHOUSE_TEXT_MOMENT_TEMPLATE.format(trimmed_sql)} AS Nullable({column_type}))"
        # A text converted into a Nullable type is NULL of bad input - a NULL's text is read as 0.
        number_sql = cls.get_number_sql(f"ifNull({trimmed_sql}, '0')", source, function, column_type, keeps_null=False)
        return f"if(isNull({term_sql}), NULL, {number_sql})"

    @staticmethod
    def get_number_sql(
        term_sql: str, source: CastType, function: CastTo, column_type: str, *, keeps_null: bool
    ) -> str:
        """A number - an integer of a float rounded half to even and of a Decimal half away from zero, a
        Decimal rounded half away from zero to the target's places (of a float by its 15 significant
        digits); one out of the target's range is refused.

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
            if source is CastType.FLOAT:
                term_sql = f"roundBankers({term_sql})"
            elif source is CastType.DECIMAL:
                term_sql = f"round({term_sql})"
            return f"accurateCast({term_sql}, '{f'Nullable({column_type})' if keeps_null else column_type}')"
        max_digits, decimal_places = function.parameters
        if max_digits is None or decimal_places is None:
            return f"CAST({term_sql} AS Nullable({column_type}))"
        if source is CastType.INTEGER:
            term_sql = f"toDecimal256({term_sql}, 0)"
        elif source is CastType.FLOAT:
            term_sql = CLICKHOUSE_FLOAT_DECIMAL_TEMPLATE.format(term_sql)
        elif source is not CastType.DECIMAL:
            term_sql = CLICKHOUSE_NUMERIC_CAST_TYPE.format(term_sql)
        return CLICKHOUSE_DECIMAL_CAST_TEMPLATE.format(
            term_sql, decimal_places, 10 ** (max_digits - decimal_places), f"Nullable({column_type})"
        )
