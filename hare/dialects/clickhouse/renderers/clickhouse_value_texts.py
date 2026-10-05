from __future__ import annotations

from hare.dialects.clickhouse.constants import CLICKHOUSE_DATETIME_PRECISION
from hare.dialects.clickhouse.renderers.constants import (
    CLICKHOUSE_DATETIME_TEXT_FORMAT,
    CLICKHOUSE_DECIMAL_TEXT_TEMPLATE,
    CLICKHOUSE_FLOAT_TEXT_TEMPLATE,
    CLICKHOUSE_MOMENT_FRACTION_ZEROS_PATTERN,
    CLICKHOUSE_TIME_TEXT_TEMPLATE,
    CLICKHOUSE_TYPE_SCALE_TEMPLATE,
    CLICKHOUSE_UTC_OFFSET_TEXT,
)
from hare.sql.enums import CastType


class ClickhouseValueTexts:
    """A value as PostgreSQL writes it as text - what ``Cast()`` to a text gives on every backend:
    a double in its shortest digits, scientific outside 1e-4 to 1e15; a decimal with all the places of
    its type; a moment and a time of day without trailing zeros in their fraction, an aware one with
    its offset in hours."""

    @classmethod
    def get_text_sql(cls, term_sql: str, source: CastType, *, is_aware: bool, max_length: int | None) -> str:
        """The SQL of a value's text.

        Args:
            term_sql: The value.
            source: The type of the value.
            is_aware: Whether a moment is written with its offset.
            max_length: The most characters kept - None for all.

        Returns:
            The SQL.
        """
        if source == CastType.FLOAT:
            text_sql = CLICKHOUSE_FLOAT_TEXT_TEMPLATE.format(term_sql)
        elif source == CastType.DECIMAL:
            text_sql = cls.get_decimal_text_sql(term_sql)
        elif source == CastType.DATETIME:
            text_sql = cls.get_moment_text_sql(term_sql, is_aware=is_aware)
        elif source == CastType.TIME:
            text_sql = CLICKHOUSE_TIME_TEXT_TEMPLATE.format(term_sql)
        else:
            text_sql = f"toString({term_sql})"
        return text_sql if max_length is None else f"substringUTF8({text_sql}, 1, {int(max_length)})"

    @staticmethod
    def get_decimal_text_sql(term_sql: str) -> str:
        """A decimal's text with every place of its type.

        Args:
            term_sql: The value.

        Returns:
            The SQL.
        """
        return CLICKHOUSE_DECIMAL_TEXT_TEMPLATE.format(term_sql, CLICKHOUSE_TYPE_SCALE_TEMPLATE.format(term_sql))

    @staticmethod
    def get_moment_text_sql(term_sql: str, *, is_aware: bool) -> str:
        """A moment's text in UTC - with its offset when aware, its wall clock otherwise.

        Args:
            term_sql: The moment.
            is_aware: Whether it is written with its offset.

        Returns:
            The SQL.
        """
        text_sql = (
            f"replaceRegexpOne(formatDateTime(toDateTime64({term_sql}, {CLICKHOUSE_DATETIME_PRECISION}, 'UTC'), "
            f"'{CLICKHOUSE_DATETIME_TEXT_FORMAT}', 'UTC'), '{CLICKHOUSE_MOMENT_FRACTION_ZEROS_PATTERN}', '')"
        )
        return f"concat({text_sql}, '{CLICKHOUSE_UTC_OFFSET_TEXT}')" if is_aware else text_sql
