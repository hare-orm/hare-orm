from __future__ import annotations

import datetime

import asyncpg

from hare.dialects.postgresql.drivers.asyncpg.constants import (
    POSTGRES_INTERVAL_DAYS_PER_MONTH,
    POSTGRES_INTERVAL_DAYS_PER_YEAR,
    POSTGRES_INTERVAL_MONTHS_PER_YEAR,
)
from hare.fields.data.temporal.time_delta_field import TimeDeltaField


class AsyncpgIntervalCodec:
    """The ``interval`` codec every asyncpg connection gets - asyncpg's own conversion, a year of 365
    days and a month of 30, computed without its overflow: asyncpg reads an interval of more than
    about 5.9 million years as a wrong ``timedelta``. hare's own queries read no interval - a
    duration is a number of microseconds - so only raw SQL decodes through it."""

    @staticmethod
    def encode(value: datetime.timedelta) -> tuple[int, int, int]:
        """Encodes a timedelta as Postgres's binary ``interval``, as asyncpg does.

        Args:
            value: The bound timedelta.

        Returns:
            Months (none), days and microseconds.

        Raises:
            TypeError: The value isn't a timedelta.
        """
        if not isinstance(value, datetime.timedelta):
            raise TypeError(f"expected a datetime.timedelta instance, got {type(value).__name__!r}")
        return (0, value.days, TimeDeltaField.get_microseconds(value - datetime.timedelta(days=value.days)))

    @staticmethod
    def decode(value: tuple[int, int, int]) -> datetime.timedelta:
        """Decodes Postgres's binary ``interval`` into a timedelta.

        Args:
            value: Months, days and microseconds.

        Returns:
            The timedelta.

        Raises:
            OverflowError: The interval is beyond a timedelta's range - an infinity among them.
        """
        months, days, microseconds = value
        # Months truncated towards zero into years, as asyncpg divides them.
        years = abs(months) // POSTGRES_INTERVAL_MONTHS_PER_YEAR * (1 if months >= 0 else -1)
        remaining_months = months - years * POSTGRES_INTERVAL_MONTHS_PER_YEAR
        total_days = (
            years * POSTGRES_INTERVAL_DAYS_PER_YEAR + remaining_months * POSTGRES_INTERVAL_DAYS_PER_MONTH + days
        )
        try:
            return datetime.timedelta(days=total_days, microseconds=microseconds)
        except OverflowError:
            raise OverflowError(f"interval of {total_days} days is out of range for a Python timedelta") from None

    @classmethod
    async def install(cls, connection: asyncpg.Connection) -> None:
        """Registers the codec on a new connection.

        Args:
            connection: The connection.
        """
        await connection.set_type_codec(
            "interval", schema="pg_catalog", encoder=cls.encode, decoder=cls.decode, format="tuple"
        )
