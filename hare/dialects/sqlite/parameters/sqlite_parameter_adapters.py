from __future__ import annotations

import datetime
from decimal import Decimal

from hare.dialects.sqlite.constants import SQLITE_DECIMAL_FIXED_POINT_MAX_EXPONENT


class SqliteParameterAdapters:
    """The text SQLite stores for Python values it has no native type for."""

    @staticmethod
    def adapt_decimal(value: Decimal) -> str:
        """Decimal text in fixed-point notation, as Postgres prints a numeric.

        Args:
            value: The bound decimal.

        Returns:
            ``0.0000000001`` rather than ``1E-10``; scientific text for a non-finite value or an
            exponent past ``SQLITE_DECIMAL_FIXED_POINT_MAX_EXPONENT``.
        """
        if (
            value.is_finite()
            and value.adjusted() <= SQLITE_DECIMAL_FIXED_POINT_MAX_EXPONENT
            and int(value.as_tuple().exponent) >= -SQLITE_DECIMAL_FIXED_POINT_MAX_EXPONENT
        ):
            return format(value, "f")
        return str(value)

    @staticmethod
    def adapt_datetime(value: datetime.datetime) -> str:
        """ISO text of a datetime, an aware one converted to UTC first.

        SQLite has no ``TIMESTAMPTZ``: ordering, comparisons and ``Min``/``Max`` compare the stored
        text, which only matches chronological order when every row carries the same offset.

        Args:
            value: The bound datetime.

        Returns:
            ``2024-06-01 08:00:00+00:00`` for an aware value, the naive wall-clock text otherwise.
        """
        if value.tzinfo is not None:
            value = value.astimezone(datetime.UTC)
        return value.isoformat(" ")

    @staticmethod
    def adapt_time(value: datetime.time) -> str:
        """ISO text of a time, keeping an aware value's own offset (as Postgres's ``TIMETZ`` does).

        Args:
            value: The bound time.

        Returns:
            ``12:30:45``, ``12:30:45.000250`` or ``12:30:45+03:00``.
        """
        return value.isoformat()
